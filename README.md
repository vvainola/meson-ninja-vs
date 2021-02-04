# Visual Studio solution generation for Meson Ninja backend

## Overview
Generate Visual Studio solution for [Meson](https://mesonbuild.com/) build system also with Ninja backend. The Visual Studio solution is only a wrapper for the Ninja build. Meson has native Visual Studio backend support but it is not as mature as the Ninja backend. In addition, using a compiler cache [sccache](https://github.com/mozilla/sccache/) is difficult with MSBuild. Using the Ninja backend allows setting the cache executable before generating build directory with
```
CXX="sccache cl"
```
but still having the debugging capabilities of Visual Studio.

## Usage
```
python ninja-vs.py --build_root path_to_build_root
```
The build root then contains the Visual Studio solution with same name as the project. Headers from source directory are automatically included after first build.


## Known issues
* Executables are not automatically built before execution if build is out-of-date. By default "Build Solution" calls Ninja without specifying any targets. Other targets are not automatically built because that would make MSBuild call all other projects in parallel which messes up the Ninja build. If the "all projects" build is added as dependency for individual projects, it would no longer be possible to build single project at a time.
