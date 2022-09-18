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

vs_header_tmpl = """<?xml version="1.0" ?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003" DefaultTargets="Build" ToolsVersion="4.0">
\t<ItemGroup Label="ProjectConfigurations">
\t\t<ProjectConfiguration Include="{configuration}|{platform}">
\t\t\t<Configuration>{configuration}</Configuration>
\t\t\t<Platform>{platform}</Platform>
\t\t</ProjectConfiguration>
\t</ItemGroup>\n"""

vs_globals_tmpl = """\t<PropertyGroup Label="Globals">
\t\t<ProjectGuid>{guid}</ProjectGuid>
\t\t<Keyword>{platform}Proj</Keyword>
\t\t<Platform>{platform}</Platform>
\t\t<ProjectName>{name}</ProjectName>
\t</PropertyGroup>
\t<ItemDefinitionGroup>
\t\t<Link>
\t\t\t<SubSystem>Console</SubSystem>
\t\t</Link>
\t</ItemDefinitionGroup>
\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.Default.props"/>\n"""

vs_config_tmpl = """\t<PropertyGroup Label="Configuration">
\t\t<PlatformToolset>v143</PlatformToolset>
\t\t<ConfigurationType>{config_type}</ConfigurationType>
\t</PropertyGroup>
\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.props"/>\n"""

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
\t\t\t<Outputs>{out_file}</Outputs>
\t\t\t<AdditionalInputs>{additional_inputs}</AdditionalInputs>
\t\t\t<VerifyInputsAndOutputsExist>{verify_io}</VerifyInputsAndOutputsExist>
\t\t</CustomBuild>
\t</ItemDefinitionGroup>
\t<ItemGroup>
\t\t<CustomBuild Include="{contents}">
\t\t</CustomBuild>
\t</ItemGroup>\n"""

vs_dependency_tmpl = """\t\t<ProjectReference Include="{vcxproj_name}">
\t\t\t<Project>{{{project_guid}}}</Project>
\t\t\t<LinkLibraryDependencies>{link_deps}</LinkLibraryDependencies>
\t\t</ProjectReference>\n"""

vs_end_proj_tmpl = """\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.targets"/>
\t<ImportGroup Label="ExtensionTargets"/>
</Project>"""

vs_start_filter = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
\t<ItemGroup>\n"""

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


class BuildTarget:
    def __init__(self, intro_target, guid, build_dir):
        self.name = intro_target['name']
        self.id = intro_target['id']
        self.guid = guid
        self.type = intro_target['type']
        self.build_by_default = intro_target['build_by_default']
        target_sources = intro_target['target_sources']
        self.sources = []
        self.parameters = []
        if target_sources != []:
            target_sources = target_sources[0]
            self.sources.extend(target_sources['sources'])
            self.sources.extend(target_sources['generated_sources'])
            self.parameters.extend(target_sources['parameters'])
        self.extra_files = intro_target.get('extra_files', [])
        if len(intro_target['filename']) == 1:
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


def try_find_file(source_dir, filename):
    # check source dir
    for f in glob.glob(f'{source_dir}/**/{filename}', recursive=True):
        return f
    try:
        # try PATH
        f = subprocess.check_output(['where', f'{filename}'], stderr=subprocess.DEVNULL).decode('utf-8').strip()
        return f
    except subprocess.SubprocessError:
        return "None"


def get_headers(intro):
    build_dir = Path(intro['meson_info']['directories']['build'])
    source_dir = Path(intro['meson_info']['directories']['source'])
    targets = intro['targets']
    target_headers = {}
    for target in targets:
        target_headers[f'{target["name"]}'] = set()
    # Ask list of headers used in object from ninja
    object_deps = (
        subprocess.check_output(['ninja', '-C', build_dir, '-t', 'deps'])
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
                    filt_headers.append(h.absolute().resolve())
            except ValueError:
                pass
        filt_target_headers[target] = filt_headers
    return filt_target_headers


def generate_guid():
    return str(uuid.uuid4()).upper()


def generate_guid_from_path(path):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(path))).upper()


def get_introspect_files(build_dir):
    intro = {}
    prefix = build_dir / 'meson-info'
    intro['benchmarks'] = prefix / 'intro-benchmarks.json'
    intro['buildoptions'] = prefix / 'intro-buildoptions.json'
    intro['buildsystem_files'] = prefix / 'intro-buildsystem_files.json'
    intro['dependencies'] = prefix / 'intro-dependencies.json'
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

def run_reconfigure(build_dir):
    build_dir = Path(build_dir)
    # Collect options into a dict with name for easier lookup
    intro = get_introspect_files(build_dir)
    buildoptions = {}
    for opt in intro['buildoptions']:
        buildoptions[opt['name']] = opt

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
        opt_name = opt_name.group(0).replace("__", ".").replace("-", ":")
        opt_value = opt_value.group(0)
        if opt_value != str(buildoptions[opt_name]['value']):
            changed_options.append(f'-D{opt_name}=\"{opt_value}\"')
    meson = get_meson_command(build_dir)
    if changed_options != []:
        configure = f'{meson} configure {" ".join(changed_options)}'
        print(configure)
        print(subprocess.check_output(configure, cwd=build_dir).decode('utf-8').replace('\r', ''))
    print(
        subprocess.check_output(f'ninja build.ninja', cwd=build_dir)
        .decode('utf-8')
        .replace('\r', '')
    )


class VisualStudioSolution:
    def __init__(self, build_dir):
        self.build_dir = Path(build_dir)
        if not (Path(build_dir).is_absolute()):
            self.build_dir = self.build_dir.absolute()
        cl_location = shutil.which('cl')
        if cl_location == None:
            sys.exit("cl.exe not found from PATH. Are you running from VS developer command prompt?")
        arch = os.path.basename(os.path.dirname(cl_location))
        if arch == 'x86':
            self.platform = 'Win32'
        else:
            self.platform = 'x64'
        self.vcxprojs = []

        self.intro = get_introspect_files(self.build_dir)
        for option in self.intro['buildoptions']:
            if option['name'] == 'buildtype':
                self.build_type = option['value']
        self.source_dir = self.intro['meson_info']['directories']['source']
        self.subdirs = set()
        build_to_run_subdir = "Build to run"
        self.subdirs.add(build_to_run_subdir)

        self.headers = get_headers(self.intro)

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
        self.generate_ninja_proj(self.ninja_proj)
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
        self.generate_run_proj(install_proj, f'ninja install')
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
        self.generate_run_proj(test_proj, f'ninja test')
        # Regen
        self.regen_proj = VcxProj(
            "Regenerate solution",
            "Regenerate_solution",
            generate_guid_from_path(self.build_dir / 'regen'),
            build_by_default=True,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(self.regen_proj)
        self.generate_regen_proj(self.regen_proj)
        # Reconfigure
        self.reconfigure_proj = VcxProj(
            "Reconfigure project",
            "Reconfigure_project",
            generate_guid_from_path(self.build_dir / 'reconfigure'),
            build_by_default=False,
            is_run_target=True,
            subdir=build_to_run_subdir,
        )
        self.vcxprojs.append(self.reconfigure_proj)
        self.generate_reconfigure_proj(self.reconfigure_proj)

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
                self.generate_run_proj(vcxproj, f'ninja {target["name"]}')
            else:
                self.generate_build_proj(BuildTarget(target, guid, self.build_dir))
        self.generate_solution(self.intro['projectinfo']['descriptive_name'] + '.sln')

    def write_basic_custom_build(self, proj, command, additional_inputs="", verify_io=False):
        proj_file = open(f'{self.build_dir}/{proj.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(configuration=self.build_type, platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(guid=proj.guid, platform=self.platform, name=proj.name))
        proj_file.write(vs_config_tmpl.format(config_type="Utility"))
        proj_file.write(
            vs_propertygrp_tmpl.format(out_dir='.\\', intermediate_dir=f'{proj.id}_temp\\', output=f'{proj.id}')
        )
        proj_contents = f'{proj.id}_temp\\always_rebuild_{proj.id}.rule'
        proj_output = f'{proj.id}_temp\\always_rebuild_{proj.id}.out'
        proj_file.write(
            vs_custom_itemgroup_tmpl.format(
                command=command,
                additional_inputs=additional_inputs,
                out_file=proj_output,
                contents=proj_contents,
                verify_io=verify_io,
            )
        )
        if not (self.build_dir / proj_contents).exists():
            if not (self.build_dir / proj_contents).parents[0].exists():
                os.makedirs((self.build_dir / proj_contents).parents[0])
            open(self.build_dir / proj_contents, 'w', encoding='utf-8').close()
        if verify_io:
            open(self.build_dir / proj_output, 'w', encoding='utf-8').close()
        return proj_file

    def generate_run_proj(self, proj: VcxProj, cmd):
        proj_file = self.write_basic_custom_build(proj, command=cmd + " $(LocalDebuggerCommandArguments)")
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_ninja_proj(self, proj: VcxProj):
        proj_file = self.write_basic_custom_build(
            proj,
            command=f'ninja $(LocalDebuggerCommandArguments)',
        )
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_regen_proj(self, proj):
        proj_file = self.write_basic_custom_build(
            proj,
            command=f'{sys.executable} {os.path.abspath(__file__)} --build_root &quot;{self.build_dir}&quot;',
            additional_inputs="build.ninja",
            verify_io=True,
        )
        proj_file.write('\t<ItemGroup>\n')
        proj_file.write(
            vs_dependency_tmpl.format(
                vcxproj_name=f'{self.ninja_proj.id}.vcxproj', project_guid=self.ninja_proj.guid, link_deps='false'
            )
        )
        proj_file.write('\t</ItemGroup>\n')
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_reconfigure_proj(self, proj: VcxProj):
        # Create rule with options
        rule = open(f'{self.build_dir}/meson_options.xml', 'w', encoding='utf-8')
        rule.write(vs_meson_options_rule)
        rule.write('\t<Rule.Categories>\n')
        added_categories = []
        for opt in self.intro['buildoptions']:
            category = opt['section']
            if category not in added_categories:
                added_categories.append(category)
                rule.write(f'\t\t<Category Name="{category}" DisplayName="{category}" Description="" />\n')
        rule.write('\t</Rule.Categories>\n')
        for opt in self.intro['buildoptions']:
            opt_name = opt['name'].replace('.', '__').replace(":", "-")
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
        proj_file = self.write_basic_custom_build(
            proj,
            command=f'{sys.executable} {os.path.abspath(__file__)} --reconfigure --build_root=&quot;{self.build_dir}&quot;',
        )
        proj_file.write('\t<PropertyGroup>\n')
        for opt in self.intro['buildoptions']:
            opt_name = opt["name"].replace(".", "__").replace(":", "-")
            proj_file.write(f'\t\t<meson_{opt_name}>{opt["value"]}</meson_{opt_name}>\n')
        proj_file.write('\t\t<UseDefaultPropertyPageSchemas>false</UseDefaultPropertyPageSchemas>')
        proj_file.write('\t</PropertyGroup>\n')
        proj_file.write(vs_include_meson_options)

        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_build_proj(self, target: BuildTarget):
        proj_file = open(f'{self.build_dir}/{target.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(configuration=self.build_type, platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(guid=target.guid, name=target.name, platform=self.platform))

        # NMake
        proj_file.write(vs_config_tmpl.format(config_type="MakeFile"))
        include_paths = []
        preprocessor_macros = []
        additional_options = []
        for par in target.parameters:
            if par.startswith('-I') or par.startswith('/I'):
                include_paths.append(par[2:])
            elif par.startswith('-D') or par.startswith('/D'):
                preprocessor_macros.append(par[2:])
            else:
                additional_options.append(par)
        compile = f'ninja -C &quot;{self.build_dir}&quot;'
        proj_file.write(
            vs_nmake_tmpl.format(
                output=os.path.basename(target.output),
                build_cmd=f'{compile} &quot;{target.output}&quot;',
                clean_cmd=f'{compile} clean',
                rebuild_cmd=f'{compile} clean \n {compile} &quot;{target.output}&quot;',
                includes=";".join(include_paths),
                preprocessor_macros=";".join(preprocessor_macros),
                additional_options=" ".join(additional_options),
            )
        )
        # Files
        proj_file.write('\t<ItemGroup>\n')
        for src in target.sources + target.extra_files + self.headers[target.name]:
            proj_file.write(f'\t\t<ClCompile Include="{src}">\n')
            proj_file.write(
                f'\t\t\t<AdditionalIncludeDirectories>{";".join(include_paths)}</AdditionalIncludeDirectories>\n'
            )
            proj_file.write(f'\t\t</ClCompile>\n')
        proj_file.write('\t</ItemGroup>\n')
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

        # Add filter file so that headers are in own folder
        filter_file = open(f'{self.build_dir}/{target.id}.vcxproj.filters', 'w', encoding='utf-8')
        filter_file.write(vs_start_filter)
        for src in target.sources + target.extra_files:
            filter_file.write(f'\t\t<ClCompile Include="{src}"/>\n')
        for h in self.headers[target.name]:
            filter_file.write(f'\t\t<ClCompile Include="{h}">\n')
            filter_file.write(f'\t\t\t<Filter>Headers</Filter>\n')
            filter_file.write(f'\t\t</ClCompile>\n')
        filter_file.write('\t</ItemGroup>\n')
        filter_file.write('\t<ItemGroup>\n')
        filter_file.write('\t\t<Filter Include="Headers">\n')
        filter_file.write(f'\t\t\t<UniqueIdentifier>{{{generate_guid()}}}</UniqueIdentifier>\n')
        filter_file.write('\t\t</Filter>\n')
        filter_file.write('\t</ItemGroup>\n')
        filter_file.write('</Project>\n')

    def generate_solution(self, sln_filename):
        sln = open(f'{self.build_dir}/{sln_filename}', 'w', encoding='utf-8')
        sln.write('Microsoft Visual Studio Solution File, Format Version 12.00\n')
        sln.write('# Visual Studio 2019\n')
        for proj in self.vcxprojs:
            sln.write(f'Project("{cpp_guid}") = "{proj.name}", "{proj.id}.vcxproj", "{{{proj.guid}}}"\n')
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
            if proj == self.regen_proj or proj == self.ninja_proj:
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create Visual Studio solution with ninja backend.')
    parser.add_argument('-b', '--build_root', type=str, help='Path to build directory root')
    parser.add_argument('--reconfigure', action='store_true', help='Run reconfigure in the build root')
    args = parser.parse_args()

    if args.reconfigure:
        run_reconfigure(args.build_root)
    VisualStudioSolution(args.build_root)
