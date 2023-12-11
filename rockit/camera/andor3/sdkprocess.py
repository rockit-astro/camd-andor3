#!/usr/bin/env python3
#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Daemon for controlling the Warwick one-metre telescope cameras via Pyro"""

# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
# pylint: disable=too-many-lines
# pylint: disable=too-many-instance-attributes
# pylint: disable=bare-except

from ctypes import c_uint8
import json
import pathlib
import sys
import threading
import time
import traceback
import numpy as np
import pyAndorSDK3
from astropy.time import Time
import astropy.units as u
from rockit.common import log
from .constants import CommandStatus, CameraStatus


class SDKInterface:
    def __init__(self, config, processing_queue, processing_framebuffer, processing_framebuffer_offsets,
                 processing_stop_signal):
        self._config = config
        self._status_condition = threading.Condition()
        self._command_lock = threading.Lock()

        self._sdk = pyAndorSDK3.AndorSDK3()
        self._cam = None

        self._camera_model = ''
        self._camera_firmware_version = ''
        self._sdk_version = self._sdk.SoftwareVersion

        self._readout_width = 0
        self._readout_height = 0

        self._temperature = 0
        self._temperature_status = None
        self._temperature_locked = False
        self._cooler_enabled = False
        self._target_temperature = config.temperature_setpoint

        # Crop output data to detector coordinates
        self._window_region = [0, 0, 0, 0]
        self._image_region = [0, 0, 0, 0]
        self._binning = config.binning

        self._exposure_time = 1

        # Limit and number of frames acquired during the next sequence
        # Set to 0 to run continuously
        self._sequence_frame_limit = 0

        # Number of frames acquired this sequence
        self._sequence_frame_count = 0

        # Time that the latest frame in the exposure was started
        self._sequence_exposure_start_time = Time.now()

        # Information for building the output filename
        self._output_directory = pathlib.Path(config.output_path)
        self._output_frame_prefix = config.output_prefix

        # Persistent frame counters
        self._counter_filename = config.expcount_path
        try:
            with open(self._counter_filename, 'r', encoding='utf-8') as infile:
                data = json.load(infile)
                self._exposure_count = data['exposure_count']
                self._exposure_count_reference = data['exposure_reference']
        except Exception:
            self._exposure_count = 0
            self._exposure_count_reference = Time.now().strftime('%Y-%m-%d')

        # Thread that runs the exposure sequence
        # Initialized by start() method
        self._acquisition_thread = None

        # Signal that the exposure sequence should be terminated
        # at end of the current frame
        self._stop_acquisition = False

        # Subprocess for processing acquired frames
        self._processing_queue = processing_queue
        self._processing_stop_signal = processing_stop_signal

        # A large block of shared memory for sending frame data to the processing workers
        self._processing_framebuffer = processing_framebuffer

        # A queue of memory offsets that are available to write frame data into
        # Offsets are popped from the queue as new frames are written into the
        # frame buffer, and pushed back on as processing is complete
        self._processing_framebuffer_offsets = processing_framebuffer_offsets

        # Thread for polling camera status
        threading.Thread(target=self.__poll_camera_status, daemon=True).start()

    @property
    def is_acquiring(self):
        return self._acquisition_thread is not None and self._acquisition_thread.is_alive()

    def __poll_camera_status(self):
        """Background thread that polls the camera status"""
        while True:
            # Take a copy to avoid race conditions with camera shutdown
            cam = self._cam
            if cam is not None:
                try:
                    # Query temperature status
                    self._temperature = cam.SensorTemperature
                    self._temperature_status = cam.TemperatureStatus.upper()
                    self._temperature_locked = self._temperature_status == 'STABILISED'
                    self._cooler_enabled = cam.SensorCooling
                except Exception as e:
                    print('Failed to query temperature with error', e)

            time.sleep(self._config.temperature_query_delay)

    def __run_exposure_sequence(self, quiet):
        """Worker thread that acquires frames and their times.
           Tagged frames are pushed to the acquisition queue
           for further processing on another thread"""
        processing = 0
        try:
            self._cam.ExposureTime = float(self._exposure_time)
            self._cam.CycleMode = 'Continuous'
            self._cam.TriggerMode = 'Internal'
            self._cam.MetadataEnable = True
            self._cam.MetadataTimestamp = True

            reference_time = Time.now()
            self._cam.TimestampClockReset()
            tick_frequency = self._cam.TimestampClockFrequency
            encoding = self._cam.PixelEncoding.upper()
            exposure = self._cam.ExposureTime
            frameperiod = 1.0 / self._cam.FrameRate
            rowperiod = self._cam.RowReadTime

            # Prepare the framebuffer offsets
            if not self._processing_framebuffer_offsets.empty():
                log.error(self._config.log_name, 'Frame buffer offsets queue is not empty!')
                return

            offset = 0
            frame_size = self._cam.ImageSizeBytes
            buffers = []
            while offset + frame_size <= len(self._processing_framebuffer):
                cdata = (c_uint8 * frame_size).from_buffer(self._processing_framebuffer, offset)
                buffer = np.ctypeslib.as_array(cdata)
                self._cam.queue(buffer, frame_size)
                buffers.append(buffer)
                offset += frame_size

            self._cam.AcquisitionStart()
            while not self._stop_acquisition and not self._processing_stop_signal.value:
                while not self._processing_framebuffer_offsets.empty():
                    processing -= 1
                    offset = self._processing_framebuffer_offsets.get(block=False)
                    self._cam.queue(buffers[offset], frame_size)

                self._sequence_exposure_start_time = Time.now()
                acq = self._cam.wait_buffer(int(self._exposure_time * 1000) + 5000)
                read_end_time = Time.now()

                buffer_index = -1
                for i, buffer in enumerate(buffers):
                    # pylint: disable=protected-access
                    if acq._np_data is buffer:
                        buffer_index = i
                        break
                    # pylint: enable=protected-access

                processing += 1
                self._processing_queue.put({
                    'acquisition_buffer_index': buffer_index,
                    'acquisition_frame_size': frame_size,
                    # pylint: disable=protected-access
                    'acquisition_config': self._cam._Camera__current_config,
                    # pylint: enable=protected-access
                    'reference_time': reference_time,
                    'tick_frequency': tick_frequency,
                    'requested_exposure': self._exposure_time,
                    'exposure': exposure,
                    'frameperiod': frameperiod,
                    'rowperiod': rowperiod,
                    'encoding': encoding,
                    'read_end_time': read_end_time,
                    'sdk_version': self._sdk_version,
                    'firmware_version': self._camera_firmware_version,
                    'image_region': self._image_region,
                    'window_region': self._window_region,
                    'binning': self._binning,
                    'filter': self._config.filter,
                    'exposure_count': self._exposure_count,
                    'exposure_count_reference': self._exposure_count_reference,
                    'cooler_temperature': self._temperature,
                    'cooler_setpoint': float(self._config.temperature_setpoint),
                    'cooler_status': self._temperature_status
                })

                self._exposure_count += 1
                self._sequence_frame_count += 1

                # Continue exposure sequence?
                if 0 < self._sequence_frame_limit <= self._sequence_frame_count:
                    self._stop_acquisition = True
        finally:
            self._cam.AcquisitionStop()
            self._cam.flush()

            # Save updated counts to disk
            with open(self._counter_filename, 'w', encoding='utf-8') as outfile:
                json.dump({
                    'exposure_count': self._exposure_count,
                    'exposure_reference': self._exposure_count_reference,
                }, outfile)

            # Wait for processing to complete
            while processing > 0:
                self._processing_framebuffer_offsets.get()
                processing -= 1

            if not quiet:
                log.info(self._config.log_name, 'Exposure sequence complete')
            self._stop_acquisition = False

    def set_cooling(self, enabled, quiet):
        """Set the camera cooler"""
        try:
            self._cam.SensorCooling = enabled
            if not quiet:
                log.info(self._config.log_name, 'Sensor cooling ' + ('enabled' if enabled else 'disabled'))

            return CommandStatus.Succeeded
        except:
            return CommandStatus.Failed

    def report_status(self):
        """Returns a dictionary containing the current camera state"""
        # Estimate the current frame progress based on the time delta
        exposure_progress = 0
        sequence_frame_count = self._sequence_frame_count
        state = CameraStatus.Idle

        if self.is_acquiring:
            state = CameraStatus.Acquiring
            if self._stop_acquisition:
                state = CameraStatus.Aborting
            else:
                if self._sequence_exposure_start_time is not None:
                    exposure_progress = (Time.now() - self._sequence_exposure_start_time).to(u.s).value
                    if exposure_progress >= self._exposure_time:
                        state = CameraStatus.Reading

        return {
            'state': state,
            'cooler_enabled': self._cooler_enabled,
            'cooler_temperature': self._temperature,
            'cooler_setpoint': float(self._config.temperature_setpoint),
            'temperature_locked': self._temperature_locked,  # used by opsd
            'exposure_time': self._exposure_time,
            'exposure_progress': exposure_progress,
            'window': self._window_region,
            'binning': self._binning,
            'sequence_frame_limit': self._sequence_frame_limit,
            'sequence_frame_count': sequence_frame_count,
        }

    def initialize(self):
        """Connects to the camera driver"""
        print('initializing SDK')
        try:
            found = False
            for i in range(self._sdk.DeviceCount):
                try:
                    cam = self._sdk.GetCamera(i)
                except pyAndorSDK3.ATCoreException:
                    continue
                serial = cam.SerialNumber
                model = cam.CameraModel

                print(f'camera {i} is {model} ({serial})')
                if serial == self._config.camera_serial:
                    found = True
                    break

            if not found:
                print(f'camera with serial {self._config.camera_serial} was not found')
                return CommandStatus.CameraNotFound

            # Marana only supports temperatures of +15, -25, -40 deg C
            # Fix temperature and only expose cooling on/off
            cam.TemperatureControl = self._config.temperature_setpoint
            cam.SensorCooling = True

            cam.ExposureTime = self._exposure_time

            self._camera_model = model
            self._camera_firmware_version = cam.FirmwareVersion
            self._readout_width = cam.SensorWidth
            self._readout_height = cam.SensorHeight
            self._temperature = cam.SensorTemperature
            self._temperature_status = cam.TemperatureStatus
            self._temperature_locked = self._temperature_status == 'Stabilised'
            self._cooler_enabled = cam.SensorCooling
            self._cam = cam

            # Regions are 0-indexed x1,x2,y1,2
            # These are converted to 1-indexed when writing fits headers
            self._window_region = [
                0,
                self._readout_width - 1,
                0,
                self._readout_height - 1
            ]

            self._image_region = [
                0,
                self._readout_width - 1,
                0,
                self._readout_height - 1
            ]

            log.info(self._config.log_name, 'Initialized camera')
            return CommandStatus.Succeeded
        except Exception as e:
            self._cam = None
            log.error(self._config.log_name, 'Failed to initialize camera')
            print(e)
            return CommandStatus.Failed

    def set_exposure(self, seconds, quiet=False):
        """Set the exposure time in seconds"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if not quiet and self._exposure_time != seconds:
            log.info(self._config.log_name, f'Exposure time set to {seconds:.3f}s')

        self._exposure_time = seconds

        return CommandStatus.Succeeded

    def set_window(self, window, quiet=False):
        """Sets the sensor readout window in unbinned 1-indexed pixels"""
        def format_window(window):
            return f'[{window[0]}:{window[1]},{window[2]}:{window[3]}]'

        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        previous = format_window(self._window_region)

        if window is None:
            self._window_region = [0, self._readout_width - 1, 0, self._readout_height - 1]

        elif len(window) == 4:
            if window[0] < 1 or window[0] > self._readout_width:
                return CommandStatus.WindowOutsideSensor
            if window[1] < window[0] or window[1] > self._readout_width:
                return CommandStatus.WindowOutsideSensor
            if window[2] < 1 or window[2] > self._readout_height:
                return CommandStatus.WindowOutsideSensor
            if window[3] < window[2] or window[3] > self._readout_height:
                return CommandStatus.WindowOutsideSensor

            # Convert from 1-indexed to 0-indexed
            self._window_region = [x - 1 for x in window]
        else:
            return CommandStatus.Failed

        region = format_window(self._window_region)
        if not quiet and previous != region:
            log.info(self._config.log_name, f'Window set to {region}')

        return CommandStatus.Succeeded

    def set_binning(self, binning, quiet=False):
        """Sets the sensor binning factor"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if binning is None:
            binning = self._config.binning

        if not isinstance(binning, int) or binning < 1:
            return CommandStatus.Failed

        if not quiet and self._binning != binning:
            log.info(self._config.log_name, f'Binning set to {binning}')

        self._binning = binning
        return CommandStatus.Succeeded

    def shutdown(self):
        """Disconnects from the camera driver"""
        # Complete the current exposure
        if self._acquisition_thread is not None:
            self._cam.AcquisitionStop()
            self._cam.flush()

            print('shutdown: waiting for acquisition to complete')
            self._stop_acquisition = True
            self._acquisition_thread.join()

        print('shutdown: disconnecting SDK')
        self._cam = None

        log.info(self._config.log_name, 'Shutdown camera')
        return CommandStatus.Succeeded

    def start_sequence(self, count, quiet=False):
        """Starts an exposure sequence with a set number of frames, or 0 to run until stopped"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        self._sequence_frame_limit = count
        self._sequence_frame_count = 0
        self._stop_acquisition = False
        self._processing_stop_signal.value = False

        self._acquisition_thread = threading.Thread(
            target=self.__run_exposure_sequence,
            args=(quiet,), daemon=True)
        self._acquisition_thread.start()

        if not quiet:
            count_msg = 'until stopped'
            if count == 1:
                count_msg = '1 frame'
            elif count > 1:
                count_msg = f'{count} frames'

            log.info(self._config.log_name, f'Starting exposure sequence ({count_msg})')

        return CommandStatus.Succeeded

    def stop_sequence(self, quiet=False):
        """Stops any active exposure sequence"""
        if not self.is_acquiring or self._stop_acquisition:
            return CommandStatus.CameraNotAcquiring

        if not quiet:
            log.info(self._config.log_name, 'Aborting exposure sequence')

        self._sequence_frame_count = 0
        self._stop_acquisition = True

        self._cam.AcquisitionStop()
        self._cam.flush()

        return CommandStatus.Succeeded


def sdk_process(camd_pipe, config,
                processing_queue, processing_framebuffer, processing_framebuffer_offsets,
                stop_signal):
    cam = SDKInterface(config, processing_queue, processing_framebuffer, processing_framebuffer_offsets, stop_signal)
    ret = cam.initialize()

    camd_pipe.send(ret)
    if ret != CommandStatus.Succeeded:
        return

    try:
        while True:
            if camd_pipe.poll(timeout=1):
                c = camd_pipe.recv()
                command = c['command']
                args = c['args']

                if command == 'cooling':
                    camd_pipe.send(cam.set_cooling(args['enabled'], args['quiet']))
                elif command == 'exposure':
                    camd_pipe.send(cam.set_exposure(args['exposure'], args['quiet']))
                elif command == 'window':
                    camd_pipe.send(cam.set_window(args['window'], args['quiet']))
                elif command == 'binning':
                    camd_pipe.send(cam.set_binning(args['binning'], args['quiet']))
                elif command == 'start':
                    camd_pipe.send(cam.start_sequence(args['count'], args['quiet']))
                elif command == 'stop':
                    camd_pipe.send(cam.stop_sequence(args['quiet']))
                elif command == 'status':
                    camd_pipe.send(cam.report_status())
                elif command == 'shutdown':
                    break
                else:
                    print(f'unhandled command: {command}')
                    camd_pipe.send(CommandStatus.Failed)

    except Exception:
        traceback.print_exc(file=sys.stdout)
        camd_pipe.send(CommandStatus.Failed)

    camd_pipe.close()
    cam.shutdown()
