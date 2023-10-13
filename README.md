## Andor camera daemon

`andor3_camd` interfaces with and wraps Andor USB CMOS cameras and exposes them via Pyro.

The `cam` commandline utility for controlling the cameras is provided by [camd-client](https://github.com/rockit-astro/camd-client/).

### Configuration

Configuration is read from json files that are installed by default to `/etc/camd`.
A configuration file is specified when launching the camera server, and the `cam` frontend will search for files matching the specified camera id when launched.

The configuration options are:
```python
{
  "daemon": "ngts_marana_camera", # Run the camera server as this daemon. Daemon types are registered in `rockit.common.daemons`.
  "pipeline_daemon": "ngts_marana_pipeline", # The daemon that should be notified to hand over newly saved frames for processing.
  "pipeline_handover_timeout": 10, # The maximum amount of time to wait for the pipeline daemon to accept a newly saved frame. The exposure sequence is aborted if this is exceeded.
  "log_name": "camd_andor3@marana", # The name to use when writing messages to the observatory log.
  "control_machines": ["NGTSMaranaTCS"], # Machine names that are allowed to control (rather than just query) state. Machine names are registered in `rockit.common.IP`.
  "client_commands_module": "rockit.camera.andor3",
  "camera_serial": "CSC-00246", # Camera serial number. If not known, set a dummy value and look at the list reported when the daemon scans for cameras.
  "temperature_setpoint": "-25.0", # CMOS temperature in celsius.
  "temperature_query_delay": 10, # Amount of time in seconds to wait between querying the camera temperature and cooling status.
  "filter": "NGTS", # Value to use for the FILTER fits header keyword.
  "worker_processes":  2, # Number of processes to use for generating fits images and saving temporary images to disk.
  "framebuffer_bytes": 41943040, # Amount of shared memory to reserve for transferring frames between the camera and output processes (should be an integer multiple of frame + metadata size).
  "header_card_capacity": 144, # Pad the fits header with blank space to fit at least this many cards without reallocation.
  "binning": 1,
  "camera_id": "MARANA", # Value to use for the CAMERA fits header keyword.
  "output_path": "/var/tmp/", # Path to save temporary output frames before they are handed to the pipeline daemon. This should match the pipeline incoming_data_path setting.
  "output_prefix": "marana", # Filename prefix to use for temporary output frames.
  "expcount_path": "/var/tmp/marana-counter.json" # Path to the json file that is used to track the continuous frame and shutter numbers.
}
```

### Initial Installation

The Andor SDK is required for `rockit-camera-andor3-server`, and must be installed separately.
Download the latest SDK version from the [Docs repository](https://github.com/warwick-one-metre/docs/tree/master/andor/sdk), extract it, and then install using:
```
sudo ./install_andor
```
Select option 5 (All USB Cameras).

The automated packaging scripts will push 4 RPM packages to the observatory package repository:

| Package                          | Description                                                                             |
|----------------------------------|-----------------------------------------------------------------------------------------|
| onemetre-andor-camera-data       | Contains the json configuration files for the Warwick One Metre dual-camera instrument. |
| observatory-andor-camera-server  | Contains the `andor_camd` server and systemd service files for the camera server.       |
| python3-warwick-andor-camera     | Contains the python module with shared code.                                            |

After installing the server package, the systemd service should be enabled:
```
sudo systemctl enable --now andor3_camd@<config>
```

where `config` is `blue` then `red` for the two cameras.

Now open a port in the firewall so the TCS and dashboard machines can communicate with the camera server:
```
sudo firewall-cmd --zone=public --add-port=<port>/tcp --permanent
sudo firewall-cmd --reload
```

where `port` is the port defined in `rockit.common.daemons` for the daemon specified in the camera config.

### Upgrading Installation

New RPM packages are automatically created and pushed to the package repository for each push to the `master` branch.
These can be upgraded locally using the standard system update procedure:
```
sudo yum clean expire-cache
sudo yum update
```

The daemon should then be restarted to use the newly installed code:
```
sudo systemctl restart andor3_camd@<config>
```

### Testing Locally

The camera server and client can be run directly from a git clone:
```
./andor3_camd blue.json
CAMD_CONFIG_ROOT=. ./cam blue status
```
