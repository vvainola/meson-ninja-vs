# The MIT License
#
# Copyright (c) 2021 Vili Väinölä
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files(the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and / or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING  FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import re
from pathlib import Path
import subprocess
import argparse
import os
import sys
import json
import uuid
import shutil
import glob
import typing as T

vs_header_tmpl = """<?xml version="1.0" ?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003" DefaultTargets="Build">
\t<ItemGroup Label="ProjectConfigurations">
\t\t<ProjectConfiguration Include="{configuration}|{platform}">
\t\t\t<Configuration>{configuration}</Configuration>
\t\t\t<Platform>{platform}</Platform>
\t\t</ProjectConfiguration>
\t</ItemGroup>\n"""

vs_globals_tmpl = """\t<PropertyGroup Label="Globals">
\t\t<ProjectGuid>{{{guid}}}</ProjectGuid>
\t\t<Keyword>Win32Proj</Keyword>
\t\t<Platform>{platform}</Platform>
\t\t<ProjectName>{name}</ProjectName>
\t</PropertyGroup>
\t<Import Project="$(VCTargetsPath)\\Microsoft.Cpp.Default.props"/>\n"""

vs_config_tmpl = """\t<PropertyGroup Label="Configuration">
\t\t<PlatformToolset>{platform_toolset}</PlatformToolset>
\t\t<ConfigurationType>{config_type}</ConfigurationType>
\t</PropertyGroup>
\t<Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props"/>
\t<ImportGroup Label="ExtensionSettings"/>
\t<ImportGroup Label="Shared"/>
\t<ImportGroup Label="PropertySheets">
\t\t<Import Project="$(UserRootDir)\\Microsoft.Cpp.$(Platform).user.props" Condition="exists('$(UserRootDir)\\Microsoft.Cpp.$(Platform).user.props')" Label="LocalAppDataPlatform"/>
\t</ImportGroup>
\t<PropertyGroup Label="UserMacros"/>
\n"""

vs_propertygrp_tmpl = """\t<PropertyGroup>
\t\t<OutDir>{out_dir}</OutDir>
\t\t<IntDir>{intermediate_dir}</IntDir>
\t\t<TargetName>{output}</TargetName>
\t</PropertyGroup>\n"""

vs_nmake_tmpl = """\t<PropertyGroup>
\t\t<NMakeBuildCommandLine>{build_cmd}</NMakeBuildCommandLine>
\t\t<NMakeOutput>{output}</NMakeOutput>
\t\t<NMakeCleanCommandLine>{clean_cmd}</NMakeCleanCommandLine>
\t\t<NMakeReBuildCommandLine>{rebuild_cmd}</NMakeReBuildCommandLine>
\t\t<NMakeIncludeSearchPath>{includes}</NMakeIncludeSearchPath>
\t\t<NMakeForcedIncludes></NMakeForcedIncludes>
\t\t<NMakePreprocessorDefinitions>{preprocessor_macros}</NMakePreprocessorDefinitions>
\t\t<AdditionalOptions>{additional_options}</AdditionalOptions>
\t</PropertyGroup>\n"""

vs_custom_itemgroup_tmpl = """\t<ItemDefinitionGroup>
\t\t<CustomBuild>
\t\t\t<Command>{command}</Command>
\t\t\t<Outputs>{output}</Outputs>
\t\t\t<AdditionalInputs>{additional_inputs}</AdditionalInputs>
\t\t\t<VerifyInputsAndOutputsExist>{verify_io}</VerifyInputsAndOutputsExist>
\t\t</CustomBuild>
\t\t<ClCompile>
\t\t\t<LanguageStandard>{cpp_std}</LanguageStandard>
\t\t\t<LanguageStandard_C>{c_std}</LanguageStandard_C>
\t\t\t<Outputs></Outputs>
\t\t</ClCompile>
\t</ItemDefinitionGroup>
\t<ItemGroup>
\t\t<CustomBuild Include="{contents}" />
\t</ItemGroup>
\t<ItemDefinitionGroup>
\t\t<Link>
\t\t\t<SubSystem>Console</SubSystem>
\t\t</Link>
\t</ItemDefinitionGroup>\n"""

vs_dependency_tmpl = """\t\t<ProjectReference Include="{vcxproj_name}">
\t\t\t<Project>{{{project_guid}}}</Project>
\t\t\t<LinkLibraryDependencies>{link_deps}</LinkLibraryDependencies>
\t\t</ProjectReference>\n"""

vs_end_proj_tmpl = """\t<Import Project="$(VCTargetsPath)\\Microsoft.Cpp.targets"/>
\t<ImportGroup Label="ExtensionTargets"/>
</Project>"""

vs_start_filter = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n"""

vs_include_meson_options = """\t<ItemGroup>
\t\t<PropertyPageSchema Include="meson_options.xml">
\t\t\t<Context>Project</Context>
\t\t</PropertyPageSchema>
\t</ItemGroup>\n"""

vs_meson_options_rule = """<?xml version="1.0" encoding="utf-8"?>
\t<Rule Name="MesonConfiguration" DisplayName="Meson" PageTemplate="generic" Description="" xmlns="http://schemas.microsoft.com/build/2009/properties">
\t<Rule.DataSource>
\t\t<DataSource Persistence="ProjectFile" Label="" />
\t</Rule.DataSource>
"""

directory_guid = '{2150E333-8FDC-42A3-9474-1A3956D46DE8}'
cpp_guid = '{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}'

SET_VSCMD_VER='if not defined VSCMD_VER (set VSCMD_VER=%VISUALSTUDIOVERSION%)'
NINJA_CMD = f'{SET_VSCMD_VER} &amp;&amp; ninja'

class BuildTarget:
    def __init__(self, intro_target, guid, build_dir):
        self.name = intro_target['name']
        self.id = intro_target['id']
        self.guid = guid
        self.type = intro_target['type']
        self.build_by_default = intro_target['build_by_default']
        self.target_sources = intro_target['target_sources']
        self.extra_files = intro_target.get('extra_files', [])
        if len(intro_target['filename']) > 0:
            self.output = os.path.relpath(intro_target['filename'][0], build_dir)
        else:
            self.output = ""


class VcxProj:
    def __init__(self, name, id, guid, build_by_default, is_run_target, subdir=''):
        self.name = name
        self.id = id
        self.guid = guid
        self.build_by_default = build_by_default
        self.is_run_target = is_run_target
        self.subdir = subdir


def get_headers(intro):
    build_dir = Path(intro['meson_info']['directories']['build'])
    source_dir = Path(intro['meson_info']['directories']['source'])
    targets = intro['targets']
    target_headers = {}
    for target in targets:
        target_headers[f'{target["name"]}'] = set()
    # Ask list of headers used in object from ninja
    object_deps = (
        subprocess.check_output(['ninja', '-C', str(build_dir), '-t', 'deps'])
        .decode('utf-8')
        .replace('\r', '\n')
        .strip()
        .split('\n\n\n\n')
    )
    for dep in object_deps:
        object_name = re.match('^.*(?=: )', dep)
        if object_name == None:
            continue
        object_name = object_name.group(0)
        # Get project name in which object is included. This could use better matching if there are
        # multiple projects with same name in different folders
        target_proj = None
        for target_name, headers in target_headers.items():
            if re.match(f'.*{target_name}.*[\\/].*', object_name):
                target_proj = target_name
                break
        if target_proj == None:
            continue
        # Add headers to target
        headers = re.search('  (.|\n)*', dep)
        if headers != None:
            headers = headers.group(0).split()
            for h in headers:
                target_headers[target_name].add(Path(h))
    # Filter out headers that are not in source directory
    filt_target_headers = {}
    for target, headers in target_headers.items():
        filt_headers = []
        for h in headers:
            try:
                h_path = (build_dir / h).absolute().resolve()
                if (source_dir / h_path.relative_to(source_dir)).exists():
                    filt_headers.append(h_path.absolute().resolve())
            except ValueError:
                pass
        filt_target_headers[target] = filt_headers
    return filt_target_headers


def generate_guid():
    return str(uuid.uuid4()).upper()


def generate_guid_from_path(path):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(path))).upper()


def get_introspect_files(build_dir) -> dict:
    intro = {}
    prefix = build_dir / 'meson-info'
    intro['benchmarks'] = prefix / 'intro-benchmarks.json'
    intro['buildoptions'] = prefix / 'intro-buildoptions.json'
    intro['buildsystem_files'] = prefix / 'intro-buildsystem_files.json'
    intro['dependencies'] = prefix / 'intro-dependencies.json'
    intro['compilers'] = prefix / 'intro-compilers.json'
    intro['installed'] = prefix / 'intro-installed.json'
    intro['projectinfo'] = prefix / 'intro-projectinfo.json'
    intro['targets'] = prefix / 'intro-targets.json'
    intro['tests'] = prefix / 'intro-tests.json'
    intro['meson_info'] = prefix / 'meson-info.json'
    for key, path in intro.items():
        if not (path.exists()):
            raise Exception(f"Introspect data {path} missing!. Unable to generate Visual Studio solutions.")
        intro[key] = json.load(open(intro[key]))
    # Modify build target ids so that the VS projects are created in correct subfolder
    src_dir = intro['meson_info']['directories']['source']
    for target in intro['targets']:
        target_dir = Path(os.path.dirname(target['defined_in']))
        prefix = target_dir.relative_to(src_dir)
        target['id'] = str(prefix / target['id'])
    buildoptions = {}
    for opt in intro['buildoptions']:
        buildoptions[opt['name']] = opt
    intro['buildoptions'] = buildoptions

    # Set C and C++ in VS format e.g. c++20 -> stdcpp20
    if 'cpp_std' in intro['buildoptions']:
        intro['buildoptions']['cpp_std']['value'] = 'std' + intro['buildoptions']['cpp_std']['value'].replace('none', 'Default').replace('+', 'p')
    if 'c_std' in intro['buildoptions']:
        intro['buildoptions']['c_std']['value'] = 'std' + intro['buildoptions']['c_std']['value'].replace('none', 'Default')
    return intro


def get_meson_command(build_dir):
    with open(Path(build_dir) / 'build.ninja', 'r') as f:
        lines = f.readlines()
        for i in range(len(lines)):
            if lines[i] == "rule REGENERATE_BUILD\n":
                command = lines[i + 1].split()
                start = command.index("=") + 1
                # Sometimes the --internal flag is quoted and sometimes not
                for i in range(len(command)):
                    if "--internal" in command[i]:
                        end = i
                        break
                return " ".join(command[start:end])
    raise Exception("Unable to find meson command from build.ninja")


def get_arch(build_dir, private_dir):
    platform_arch_txt = Path(private_dir) / 'platform_arch.txt'
    if platform_arch_txt.exists():
        return platform_arch_txt.read_text()
    with open(Path(build_dir) / 'meson-logs/meson-log.txt', 'r') as f:
        txt = f.read()
        arch = re.search("(?<=(Host machine cpu: )).*$", txt, re.MULTILINE)
        if arch != None:
            platform_arch_txt.write_text(arch.group(0))
            return arch.group(0)
    raise Exception("Unable to find machine architecture from meson-log.txt")

def get_platform_toolset(intro : dict) -> str:
    cpp_compiler = intro['compilers']['build']['cpp']
    version: str = cpp_compiler['version']
    if cpp_compiler['id'] == 'msvc':
        if version.startswith("19.5"):
            return "v145"
        elif version.startswith("19.4") or version.startswith("19.3"):
            return "v143"
        elif version.startswith("19.2"):
            return "v142"
    elif cpp_compiler['id'] == 'clang-cl':
        return "ClangCL"
    raise Exception('Unable to detect machine platform toolset from intro-compilers.json')



def run_reconfigure(build_dir):
    build_dir = Path(build_dir)
    intro = get_introspect_files(build_dir)

    reconfigure_proj = build_dir / 'Reconfigure_project.vcxproj'
    proj_contents = ""
    with open(reconfigure_proj) as f:
        proj_contents = f.read()
    proj_options = re.search('<meson(.|\n)*</meson.*>', proj_contents)
    if proj_options == None:
        raise Exception("Reading meson options from Reconfigure_project.vcxproj failed")
    proj_options = proj_options.group(0).split('\n')
    changed_options = []
    for opt in proj_options:
        opt_name = re.search("(?<=(</meson_)).*(?=(>))", opt)
        opt_value = re.search("(?<=(>)).*(?=(</))", opt)
        if opt_name is None or opt_value is None:
            continue
        opt_name = opt_name.group(0).replace("__", ".").replace("--", ":")
        opt_value = opt_value.group(0)
        if opt_value != str(intro['buildoptions'][opt_name]['value']):
            changed_options.append(f'-D{opt_name}=\"{opt_value}\"')
    meson = get_meson_command(build_dir)
    if changed_options != []:
        configure = f'{meson} configure {" ".join(changed_options)}'
        print(configure)
        print(subprocess.check_output(configure, cwd=build_dir).decode('utf-8').replace('\r', ''))
    print(subprocess.check_output(f'ninja build.ninja', cwd=build_dir).decode('utf-8').replace('\r', ''))

class VisualStudioSolution:
    def __init__(self, build_dir):
        self.build_dir = Path(build_dir)
        if not (Path(build_dir).is_absolute()):
            self.build_dir = self.build_dir.absolute()
        self.tmp_dir = self.build_dir / 'ninja_vs_temp'
        self.private_dir = self.build_dir / 'ninja_vs_private'
        if not self.tmp_dir.exists():
            os.mkdir(self.tmp_dir)
        if not self.private_dir.exists():
            os.mkdir(self.private_dir)
        self.generate_python_sleep_script()
        arch = get_arch(self.build_dir, self.private_dir)
        if arch == 'x86':
            self.platform = 'Win32'
        else:
            self.platform = 'x64'
        self.vcxprojs : T.List[VcxProj] = []

        self.intro = get_introspect_files(self.build_dir)
        self.build_type = self.intro['buildoptions']['buildtype']['value']
        self.source_dir = self.intro['meson_info']['directories']['source']
        self.subdirs = set()
        build_to_run_subdir = "Build to run"
        self.subdirs.add(build_to_run_subdir)

        self.headers = get_headers(self.intro)

        # Install
        install_proj = VcxProj(
            "Run install",
            "Run_install",
            generate_guid_from_path(self.build_dir / 'install'),
            build_by_default=False,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(install_proj)
        self.generate_run_proj(install_proj, f'{NINJA_CMD} install')
        # Run tests
        test_proj = VcxProj(
            "Run tests",
            "Run_tests",
            generate_guid_from_path(self.build_dir / 'tests'),
            build_by_default=False,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(test_proj)
        self.generate_run_proj(test_proj, f'{get_meson_command(self.build_dir)} test')
        # Reconfigure
        reconfigure_proj = VcxProj(
            "Reconfigure project",
            "Reconfigure_project",
            generate_guid_from_path(self.build_dir / 'reconfigure'),
            build_by_default=False,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(reconfigure_proj)
        self.generate_reconfigure_proj(reconfigure_proj)

        # Prebuild
        self.prebuild_proj = VcxProj(
            "Prebuild",
            "Prebuild",
            generate_guid_from_path(self.build_dir / 'prebuild'),
            build_by_default=True,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        prebuild_cmd = f'del /s /q /f &quot;{self.tmp_dir}\\*&quot; > NUL'
        self.generate_run_proj(self.prebuild_proj, prebuild_cmd)
        self.vcxprojs.append(self.prebuild_proj)

        # Individual build targets
        for target in self.intro['targets']:
            subdir = os.path.dirname(os.path.relpath(target['defined_in'], self.source_dir))
            self.subdirs.add(subdir)
            guid = generate_guid_from_path(self.build_dir / target['id'])
            vcxproj = VcxProj(
                target['name'],
                target['id'],
                guid,
                build_by_default=target['build_by_default'],
                is_run_target=target['type'] == 'run',
                subdir=subdir,
            )
            self.vcxprojs.append(vcxproj)
            if vcxproj.is_run_target:
                self.generate_run_proj(vcxproj, f'{NINJA_CMD} -C &quot;{self.build_dir}&quot; {target["name"]}')
            else:
                self.generate_build_proj(vcxproj, BuildTarget(target, guid, self.build_dir))
        # Regen
        regen_proj = VcxProj(
            "Regenerate solution",
            "Regenerate_solution",
            generate_guid_from_path(self.build_dir / 'regen'),
            build_by_default=True,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(regen_proj)
        self.generate_regen_proj(regen_proj)
        # Ninja target that handles building whole solution
        self.ninja_proj = VcxProj(
            "Ninja",
            "Ninja",
            generate_guid_from_path(self.build_dir / 'ninja'),
            build_by_default=True,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(self.ninja_proj)
        ninja_cmd = f'echo NUL > &quot;{self.tmp_dir}\\ninja&quot; \n {NINJA_CMD}'
        self.generate_run_proj(self.ninja_proj, ninja_cmd, [regen_proj])
        self.generate_solution(self.intro['projectinfo']['descriptive_name'] + '.sln')

    def generate_basic_custom_build(self, proj, command, additional_inputs="", verify_io=False):
        proj_file = open(f'{self.build_dir}/{proj.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(configuration=self.build_type, platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(guid=proj.guid, platform=self.platform, name=proj.name))
        proj_file.write(vs_config_tmpl.format(config_type="Utility", platform_toolset=get_platform_toolset(self.intro)))
        # VS requires some contents in the project to be able to build it so a .dummy file is created for that
        proj_id_basename = os.path.basename(proj.id)
        proj_temp_dir = f'{proj_id_basename}_temp'
        proj_temp_dir_abs = self.build_dir / f'{proj.id}_temp'

        proj_content_file = f'run_{proj_id_basename}.dummy'
        proj_content = f'{proj_temp_dir}\\{proj_content_file}'
        proj_content_abs = proj_temp_dir_abs / proj_content_file

        proj_output_file = f'run_{proj_id_basename}.out'
        proj_output = f'{proj_temp_dir}\\{proj_output_file}'
        proj_output_abs = proj_temp_dir_abs / proj_output_file

        proj_file.write(
            vs_propertygrp_tmpl.format(
                out_dir='.\\', intermediate_dir=f'.\\{proj_temp_dir}\\', output=f'.\\{proj_id_basename}'
            )
        )
        proj_file.write(
            vs_custom_itemgroup_tmpl.format(
                command=command,
                additional_inputs=additional_inputs,
                output=proj_output,
                contents=proj_content,
                verify_io=verify_io,
                cpp_std=self.intro['buildoptions'].get('cpp_std', {}).get('value', 'Default'),
                c_std=self.intro['buildoptions'].get('c_std', {}).get('value', 'Default')
            )
        )
        # Create dummy file and output if needed
        if not proj_content_abs.exists():
            if not proj_content_abs.parents[0].exists():
                os.makedirs(proj_content_abs.parents[0])
            open(proj_content_abs, 'w', encoding='utf-8').close()
        if verify_io:
            open(proj_output_abs, 'w', encoding='utf-8').close()
        return proj_file

    def generate_run_proj(self, proj: VcxProj, cmd, dependencies=[]):
        proj_file = self.generate_basic_custom_build(proj, command=cmd + " $(LocalDebuggerCommandArguments)")

        # Dependencies
        proj_file.write('\t<ItemGroup>\n')
        for dep in dependencies:
            proj_file.write(
                vs_dependency_tmpl.format(vcxproj_name=f'{dep.id}.vcxproj', project_guid=dep.guid, link_deps='false')
            )
        proj_file.write('\t</ItemGroup>\n')
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_regen_proj(self, proj):
        proj_file = self.generate_basic_custom_build(
            proj,
            command=f'echo NUL > &quot;{self.tmp_dir}\\regen&quot; \n {NINJA_CMD} build.ninja &amp;&amp; {sys.executable} &quot;{os.path.abspath(__file__)}&quot; --build_root &quot;{self.build_dir}&quot;',
            additional_inputs=";".join(self.intro['buildsystem_files']),
            verify_io=True,
        )

        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_reconfigure_proj(self, proj: VcxProj):
        # Create rule with options
        rule = open(f'{self.build_dir}/meson_options.xml', 'w', encoding='utf-8')
        rule.write(vs_meson_options_rule)
        rule.write('\t<Rule.Categories>\n')
        added_categories = []
        for opt_name, opt in self.intro['buildoptions'].items():
            category = opt['section']
            if category not in added_categories:
                added_categories.append(category)
                rule.write(f'\t\t<Category Name="{category}" DisplayName="{category}" Description="" />\n')
        rule.write('\t</Rule.Categories>\n')
        for opt_name, opt in self.intro['buildoptions'].items():
            opt_name = opt['name'].replace('.', '__').replace(":", "--")
            opt_type = opt['type']
            category = opt['section']
            if opt_type == 'combo':
                rule.write(
                    f'\t<EnumProperty Name="meson_{opt_name}" DisplayName="{opt["name"]}" Description="{opt["description"]}" Category="{category}">\n'
                )
                for choice in opt["choices"]:
                    rule.write(f'\t\t<EnumValue Name="{choice}" DisplayName="{choice}"/>\n')
                rule.write(f'\t</EnumProperty>\n')
            elif opt_type == 'boolean':
                rule.write(
                    f'\t<EnumProperty Name="meson_{opt_name}" DisplayName="{opt["name"]}" Description="{opt["description"]}" Category="{category}">\n'
                )
                rule.write(f'\t\t<EnumValue Name="True" DisplayName="True"/>\n')
                rule.write(f'\t\t<EnumValue Name="False" DisplayName="False"/>\n')
                rule.write(f'\t</EnumProperty>\n')
            else:
                rule.write(
                    f'\t<StringProperty Name="meson_{opt_name}" DisplayName="{opt["name"]}" Category="{category}"/>\n'
                )
        rule.write('</Rule>')

        # Create the project file
        proj_file = self.generate_basic_custom_build(
            proj,
            command=f'{sys.executable} &quot;{os.path.abspath(__file__)}&quot; --reconfigure --build_root=&quot;{self.build_dir}&quot;',
        )
        proj_file.write('\t<PropertyGroup>\n')
        for opt_name, opt in self.intro['buildoptions'].items():
            opt_name = opt["name"].replace(".", "__").replace(":", "--")
            proj_file.write(f'\t\t<meson_{opt_name}>{opt["value"]}</meson_{opt_name}>\n')
        proj_file.write('\t\t<UseDefaultPropertyPageSchemas>false</UseDefaultPropertyPageSchemas>')
        proj_file.write('\t</PropertyGroup>\n')
        proj_file.write(vs_include_meson_options)

        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_build_proj(self, proj: VcxProj, target : BuildTarget):
        proj_file = open(f'{self.build_dir}/{proj.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(configuration=self.build_type, platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(guid=proj.guid, platform=self.platform, name=proj.name))
        proj_file.write(vs_config_tmpl.format(config_type="Utility", platform_toolset=get_platform_toolset(self.intro)))
        # VS requires some contents in the project to be able to build it so a .dummy file is included for that
        # but it is not created so that VS always rebuilds the target when starting debugger
        proj_id_basename = os.path.basename(proj.id)
        proj_temp_dir = f'{proj_id_basename}_temp'
        proj_content_file = f'run_{proj_id_basename}.dummy'
        proj_content = f'{proj_temp_dir}\\{proj_content_file}'

        proj_file.write(
            vs_propertygrp_tmpl.format(
                out_dir='.\\', intermediate_dir=f'.\\{proj_temp_dir}\\', output=f'{os.path.basename(target.output)}'
            )
        )

        # Single project builds are skipped when building whole solution by creating
        # a temp file, wait 200ms and check if there is more than 1 temp file. If there
        # is, it indicates that are other projects building simultaneously and the whole
        # solution will be built by separate ninja project. If there is still only 1 temp
        # file, the project has been started alone and ninja will build only that project
        ninja = f'{NINJA_CMD} -C &quot;{self.build_dir}&quot;'
        compile = f'''
&quot;{sys.executable}&quot; {self.private_dir}\\parallel_sleep.py &quot;{target.name}&quot;
if %ERRORLEVEL% == 1 ({ninja} &quot;{target.output}&quot;) else (exit /b 0)
'''
        proj_file.write(
            vs_custom_itemgroup_tmpl.format(
                command=compile,
                additional_inputs="",
                output=target.output,
                contents=proj_content,
                verify_io=False,
                cpp_std=self.intro['buildoptions'].get('cpp_std', {}).get('value', 'Default'),
                c_std=self.intro['buildoptions'].get('c_std', {}).get('value', 'Default')
            )
        )

        # Sources in json are per-language so collect all languages in case of mixed c & cpp. Adding all
        # options to project settings is wrong but intellisense does not work properly if the settings
        # are added only to file
        all_src = []
        all_include_paths = []
        all_preprocessor_macros = []
        all_additional_options = []
        lang_src = {}
        for target_src in target.target_sources:
            if 'compiler' not in target_src:
                continue
            lang = target_src['language']
            lang_src[lang] = {}
            lang_src[lang]['language'] = lang
            lang_src[lang]['includes'] = []
            lang_src[lang]['preprocessor_macros'] = []
            lang_src[lang]['additional_options'] = []
            for par in target_src['parameters']:
                if par.startswith('-I') or par.startswith('/I'):
                    all_include_paths.append(par[2:])
                    lang_src[lang]['includes'].append(par[2:])
                elif par.startswith('-D') or par.startswith('/D'):
                    define = par[2:].replace("\"", "&quot;")
                    all_preprocessor_macros.append(define)
                    lang_src[lang]['preprocessor_macros'].append(define)
                else:
                    all_additional_options.append(par)
                    lang_src[lang]['additional_options'].append(par)
            lang_src[lang]['sources'] = target_src['sources'] + target_src['generated_sources']
        proj_file.write(f'''
\t<PropertyGroup>
\t\t<IncludePath>{";".join(all_include_paths)};$(VC_IncludePath);$(WindowsSDK_IncludePath);$(IncludePath)</IncludePath>
\t</PropertyGroup>
''')


        # Files
        proj_file.write('\t<ItemGroup>\n')
        # For extra files use union of includes and preprocessor macros because the real values depend on the source file
        # so it is possible that same header is included with different macros. Some include paths need to be set to the
        # header because otherwise intellisense cannot jump from header to another header
        processed_src = set()
        for src in target.extra_files + self.headers[target.name]:
            # VS breaks if same file is included multiple times
            if src in processed_src:
                continue
            processed_src.add(src)
            proj_file.write(f'\t\t<CLInclude Include="{src}">\n')
            proj_file.write(
                f'\t\t\t<AdditionalIncludeDirectories>{";".join(all_include_paths)};%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>\n'
            )
            proj_file.write(
                f'\t\t\t<PreprocessorDefinitions>{";".join(all_preprocessor_macros)};%(PreprocessorDefinitions)</PreprocessorDefinitions>\n'
            )
            proj_file.write(f'\t\t</CLInclude>\n')
        # The lang_src contains language specific settings
        for _, lang in lang_src.items():
            for src in lang['sources']:
                all_src.append(src)
                proj_file.write(f'\t\t<ClCompile Include="{src}">\n')
                proj_file.write(
                    f'\t\t\t<AdditionalIncludeDirectories>{";".join(lang["includes"])};%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>\n'
                )
                proj_file.write(
                    f'\t\t\t<PreprocessorDefinitions>{";".join(lang["preprocessor_macros"])};%(PreprocessorDefinitions)</PreprocessorDefinitions>\n'
                )
                proj_file.write(
                    f'\t\t\t<AdditionalOptions>{" ".join(lang["additional_options"])} %(AdditionalOptions)</AdditionalOptions>\n'
                )
                proj_file.write(f'\t\t</ClCompile>\n')
        proj_file.write('\t</ItemGroup>\n')

        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

        ###############
        # Add filters to have folder structure
        ###############
        # Collect paths for filters
        src_paths = set()
        for src in all_src + self.headers[target.name]:
            path = os.path.dirname(os.path.relpath(src, self.source_dir))
            src_paths.add(path)
            # All intermediate folders need to be added as well if there are
            # subfolders with more folders but no files
            path = os.path.normpath(path)
            split_path = path.split(os.sep)
            intermediate_path = split_path[0]
            src_paths.add(intermediate_path)
            for p in split_path[1:]:
                intermediate_path += f'\\{p}'
                src_paths.add(intermediate_path)

        filter_file = open(f'{self.build_dir}/{target.id}.vcxproj.filters', 'w', encoding='utf-8')
        filter_file.write(vs_start_filter)
        filter_folder = os.path.relpath(os.path.dirname(f'{self.build_dir}/{target.id}'), self.build_dir)

        # Create filter folders
        filter_file.write('\t<ItemGroup>\n')
        for src_path in src_paths:
            if src_path == "":
                continue
            src_path = os.path.relpath(src_path, filter_folder)
            if src_path.startswith("."):
                continue
            filter_file.write(f'\t\t<Filter Include="{src_path}">\n')
            filter_file.write(f'\t\t\t<UniqueIdentifier>{{{generate_guid()}}}</UniqueIdentifier>\n')
            filter_file.write('\t\t</Filter>\n')
        filter_file.write('\t</ItemGroup>\n')

        # Add files to correct folder
        filter_file.write('\t<ItemGroup>\n')
        for f in all_src:
            filter_path = os.path.dirname(os.path.relpath(f, self.source_dir))
            if filter_path == "":
                continue
            filter_path = os.path.relpath(filter_path, filter_folder)
            filter_file.write(f'\t\t<ClCompile Include="{f}">\n')
            filter_file.write(f'\t\t\t<Filter>{filter_path}</Filter>\n')
            filter_file.write(f'\t\t</ClCompile>\n')
        for h in self.headers[target.name]:
            filter_path = os.path.dirname(os.path.relpath(h, self.source_dir))
            if filter_path == "":
                continue
            filter_path = os.path.relpath(filter_path, filter_folder)
            filter_file.write(f'\t\t<ClInclude Include="{h}">\n')
            filter_file.write(f'\t\t\t<Filter>{filter_path}</Filter>\n')
            filter_file.write(f'\t\t</ClInclude>\n')
        filter_file.write('\t</ItemGroup>\n')
        filter_file.write('</Project>\n')

    def generate_solution(self, sln_filename):
        sln = open(f'{self.build_dir}/{sln_filename}', 'w', encoding='utf-8')
        sln.write('Microsoft Visual Studio Solution File, Format Version 12.00\n')
        sln.write('# Visual Studio 2019\n')
        for proj in self.vcxprojs:
            sln.write(f'Project("{cpp_guid}") = "{proj.name}", "{proj.id}.vcxproj", "{{{proj.guid}}}"\n')
            # Add prebuild as a dependency to all other projects
            if proj != self.prebuild_proj:
                sln.write('\tProjectSection(ProjectDependencies) = postProject\n')
                sln.write(f'\t\t{{{self.prebuild_proj.guid}}} = {{{self.prebuild_proj.guid}}}\n')
                sln.write('\tEndProjectSection\n')
            sln.write('EndProject\n')
        # Targets in correct subfolder
        subdir_guids = {}
        subsubdir_parents = {}
        expanded_subdirs = set()
        for dir in self.subdirs:
            dir = dir
            if dir == '':
                continue
            split_dir = dir.split('\\')
            base = split_dir[0]
            if len(split_dir) > 1:
                expanded_subdirs.add(base)
                parent = base
                for i in range(1, len(split_dir)):
                    sub = base
                    for j in range(1, i + 1):
                        sub += "\\" + split_dir[j]
                    expanded_subdirs.add(sub)
                    subsubdir_parents[sub] = parent
                    parent = sub
            else:
                expanded_subdirs.add(dir)
        for dir in expanded_subdirs:
            guid = generate_guid_from_path(dir)
            subdir_guids[dir] = guid
            dirname = dir.split('\\')[-1]
            sln.write(f'Project("{directory_guid}") = "{dirname}", "{dirname}", "{{{guid}}}"\n')
            sln.write('EndProject\n')
        sln.write('Global\n')
        sln.write('\tGlobalSection(SolutionConfigurationPlatforms) = preSolution\n')
        sln.write(f'\t\t{self.build_type}|{self.platform} = {self.build_type}|{self.platform}\n')
        sln.write('\tEndGlobalSection\n')

        sln.write('\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n')
        for proj in self.vcxprojs:
            sln.write(
                f'\t\t{{{proj.guid}}}.{self.build_type}|{self.platform}.ActiveCfg = {self.build_type}|{self.platform}\n'
            )
            if proj.build_by_default:
                sln.write(
                    f'\t\t{{{proj.guid}}}.{self.build_type}|{self.platform}.Build.0 = {self.build_type}|{self.platform}\n'
                )
        sln.write('\tEndGlobalSection\n')

        # Run targets in "Build to run" folder
        sln.write('\tGlobalSection(NestedProjects) = preSolution\n')
        for proj in self.vcxprojs:
            if proj.subdir != '':
                sln.write(f'\t\t{{{proj.guid}}} = {{{subdir_guids[proj.subdir]}}}\n')
        for subdir, parent in subsubdir_parents.items():
            sln.write(f'\t\t{{{subdir_guids[str(subdir)]}}} = {{{subdir_guids[str(parent)]}}}\n')
        sln.write('\tEndGlobalSection\n')

        sln.write('\tGlobalSection(SolutionProperties) = preSolution\n')
        sln.write('\t\tHideSolutionNode = FALSE\n')
        sln.write('\tEndGlobalSection\n')
        sln.write('EndGlobal\n')
        sln.close()

    def generate_python_sleep_script(self):
        sleep_script = open(self.private_dir / 'parallel_sleep.py', 'w')
        tmp_dir_forward_slash = str(self.tmp_dir).replace('\\', '/')
        sleep_script.write(f'''
import time;
import os;
import sys;
open(f"{tmp_dir_forward_slash}/{{sys.argv[1]}}", "w").close()
if len(os.listdir("{tmp_dir_forward_slash}")) < 2:
    time.sleep(0.5)
sys.exit(len(os.listdir("{tmp_dir_forward_slash}")))
''')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create Visual Studio solution with ninja backend.')
    parser.add_argument('-b', '--build_root', type=str, help='Path to build directory root')
    parser.add_argument('--reconfigure', action='store_true', help='Run reconfigure in the build root')
    args = parser.parse_args()

    # Meson uses VSCMD_VER envvar to detect whether VS supports modules and sometimes this
    # envvar goes missing on regen and the regen fails.
    if 'VSCMD_VER' not in os.environ and 'VISUALSTUDIOVERSION' in os.environ:
        os.environ['VSCMD_VER'] = os.environ['VISUALSTUDIOVERSION']
    if args.reconfigure:
        run_reconfigure(args.build_root)
    VisualStudioSolution(args.build_root)
