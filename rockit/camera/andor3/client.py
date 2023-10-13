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

"""client command input handlers"""

import Pyro4
from rockit.common import TFmt
from .config import Config
from .constants import CommandStatus, CameraStatus


def run_client_command(config_path, usage_prefix, args):
    """Prints the message associated with a status code and returns the code"""
    config = Config(config_path)
    commands = {
        'bin': set_binning,
        'exposure': set_exposure,
        'start': start,
        'status': status,
        'stop': stop,
        'window': set_window,
        'cooling': set_cooling,
        'init': initialize,
        'kill': shutdown
    }

    if len(args) == 0 or (args[0] not in commands and args[0] != 'completion'):
        return print_usage(usage_prefix)

    if args[0] == 'completion':
        if 'start' in args[-2:]:
            print('continuous')
        elif 'cooling' in args[-2:]:
            print('enable disable')
        elif len(args) < 3:
            print(' '.join(commands))
        return 0

    try:
        ret = commands[args[0]](config, usage_prefix, args[1:])
    except KeyboardInterrupt:
        # ctrl-c terminates the running command
        ret = stop(config, args)

        # Report successful stop
        if ret == 0:
            ret = -100
    except Pyro4.errors.CommunicationError:
        ret = -101

    # Print message associated with error codes
    if ret not in [-1, 0]:
        print(CommandStatus.message(ret))

    return ret


def status(config, *_):
    """Reports the current camera status"""
    with config.daemon.connect() as camd:
        data = camd.report_status()

    state_desc = CameraStatus.label(data['state'], formatting=True)
    if data['state'] == CameraStatus.Acquiring:
        state_desc += f' ({TFmt.Bold}{data["exposure_progress"]:.1f} / {data["exposure_time"]:.1f}s{TFmt.Clear})'

    # Camera is disabled
    print(f'   Camera is {state_desc}')
    if data['state'] == CameraStatus.Disabled:
        return 0

    if data['state'] > CameraStatus.Idle:
        if data['sequence_frame_limit'] > 0:
            count = data['sequence_frame_count'] + 1
            limit = data['sequence_frame_limit']
            print(f'   Acquiring frame {TFmt.Bold}{count} / {limit}{TFmt.Clear}')
        else:
            print(f'   Acquiring {TFmt.Bold}UNTIL STOPPED{TFmt.Clear}')

    temperature_fmt = TFmt.Bold + TFmt.Red
    if data['temperature_locked']:
        temperature_status = TFmt.Bold + TFmt.Green + 'LOCKED' + TFmt.Clear
        temperature_fmt = TFmt.Bold + TFmt.Green
    elif not data['cooler_enabled']:
        temperature_status = TFmt.Bold + TFmt.Red + 'COOLING DISABLED' + TFmt.Clear
        temperature_fmt = TFmt.Bold
    else:
        temperature_status = f'{TFmt.Bold}LOCKING ON {data["cooler_setpoint"]:.0f}\u00B0C{TFmt.Clear}'

    print(f'   Temperature is {temperature_fmt}{data["cooler_temperature"]:.0f}\u00B0C{TFmt.Clear} ({temperature_status})')

    w = [x + 1 for x in data['window']]
    print(f'   Output Window is {TFmt.Bold}[{w[0]}:{w[1]},{w[2]}:{w[3]}] px{TFmt.Clear}')
    print(f'   Binning is {TFmt.Bold}{data["binning"]} x {data["binning"]} px{TFmt.Clear}')
    print(f'   Exposure time is {TFmt.Bold}{data["exposure_time"]:.2f} s{TFmt.Clear}')
    return 0


def set_exposure(config, usage_prefix, args):
    """Set the camera exposure time"""
    if len(args) == 1:
        exposure = float(args[0])
        with config.daemon.connect() as camd:
            return camd.set_exposure(exposure)
    print(f'usage: {usage_prefix} exposure <seconds>')
    return -1


def set_cooling(config, usage_prefix, args):
    """Set the camera cooling mode"""
    if len(args) == 1 and (args[0] == 'enable' or args[0] == 'disable'):
        enabled = args[0] == 'enable'
        with config.daemon.connect() as camd:
            return camd.set_cooling(enabled)
    print(f'usage: {usage_prefix} cooling (enable|disable)')
    return -1


def set_binning(config, usage_prefix, args):
    """Set the camera binning"""
    if len(args) == 1:
        # Assume square pixels
        binning = int(args[0])
        with config.daemon.connect() as camd:
            return camd.set_binning(binning, binning)
    print(f'usage: {usage_prefix} bin <pixel size>')
    return -1


def set_window(config, usage_prefix, args):
    """Set the camera readout window"""
    window = None
    if len(args) == 4:
        window = [
            int(args[0]),
            int(args[1]),
            int(args[2]),
            int(args[3])
        ]

    if window or (len(args) == 1 and args[0] == 'default'):
        with config.daemon.connect() as camd:
            return camd.set_window(window)

    print(f'usage: {usage_prefix} window (<x1> <x2> <y1> <y2>|default)')
    return -1


def start(config, usage_prefix, args):
    """Starts an exposure sequence"""
    if len(args) == 1:
        try:
            count = 0 if args[0] == 'continuous' else int(args[0])
        except Exception:
            print('error: invalid exposure count:', args[0])
            return -1

        if args[0] == 'continuous' or count > 0:
            with config.daemon.connect() as camd:
                return camd.start_sequence(count)

    print(f'usage: {usage_prefix} start (continuous|<count>)')
    return -1


def stop(config, *_):
    """Stops any active camera exposures"""
    with config.daemon.connect() as camd:
        return camd.stop_sequence()


def initialize(config, *_):
    """Enables the camera driver"""
    # Initialization can take more than 5 sec, so bump timeout to 20 seconds.
    with config.daemon.connect(20) as camd:
        return camd.initialize()


def shutdown(config, *_):
    """Disables the camera drivers"""
    with config.daemon.connect() as camd:
        return camd.shutdown()


def print_usage(usage_prefix):
    """Prints the utility help"""
    print(f'usage: {usage_prefix} <command> [<args>]')
    print()
    print('general commands:')
    print('   status       print a human-readable summary of the camera status')
    print('   exposure     set exposure time in seconds')
    print('   bin          set readout binning')
    print('   window       set readout window')
    print('   start        start an exposure sequence')
    print()
    print('engineering commands:')
    print('   init         initialize the camera driver')
    print('   kill         disconnect from camera driver')
    print()

    return 0
