# Visual Studio solution generation for Meson Ninja backend

## Overview
Generate Visual Studio solution for [Meson](https://mesonbuild.com/) build system also with Ninja backend. The Visual Studio solution is only a wrapper for the Ninja build. Meson has native Visual Studio backend support but it is not as mature as the Ninja backend. 

## Usage
```
python ninja_vs.py --build_root path_to_build_root
```
The build root then contains the Visual Studio solution with same name as the project. Headers from source directory are automatically included after first build.

## Features compared to native Visual Studio backend
* Enable using wrapper exe for cl.exe.
* Change Meson build options from Visual Studio GUI by changing "Reconfigure project" project properties.
* Automatically add used headers from source directory to the projects.


## Known issues
* It is not possible to have the generation as a part of a meson.build file. The generation uses introspect information that is not available when generating the build directory for the first time.
* Only Visual Studio 2022 is supported. Other versions can be used if the PlatformToolset field is corrected. Other languages than C++ have not been tested.
