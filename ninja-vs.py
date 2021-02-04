import xml.dom.minidom
import xml.etree.ElementTree as ET
import uuid
import json
import sys
import os
import argparse
import subprocess
from pathlib import Path


class BuildTarget:
    def __init__(self, intro_target, guid):
        self.name = intro_target['name']
        self.id = intro_target['id']
        self.guid = guid
        self.type = intro_target['type']
        # Build only ninja by default because otherwise
        # MSBuild will try to build vcxprojs in parallel i.e. build
        # every ninja target in parallel which will mess up the
        # build order. Ninja handles parallel build by itself
        self.build_by_default = False  # intro_target['build_by_default']
        target_sources = intro_target['target_sources']
        self.sources = []
        self.parameters = []
        if target_sources != []:
            target_sources = target_sources[0]
            self.sources.extend(target_sources['sources'])
            self.sources.extend(target_sources['generated_sources'])
            self.parameters.extend(target_sources['parameters'])
        self.extra_files = intro_target['extra_files']
        if len(intro_target['filename']) == 1:
            self.output = intro_target['filename'][0]
        else:
            self.output = ""


class VcxProj:
    def __init__(self, name, id, guid, build_by_default):
        self.name = name
        self.id = id
        self.guid = guid
        self.build_by_default = build_by_default


def get_meson_command(path_to_build_ninja):
    f = open(path_to_build_ninja, 'r')
    lines = f.readlines()
    for i in range(len(lines)):
        if lines[i] == "rule REGENERATE_BUILD\n":
            command = lines[i+1].split()
            start = command.index("=") + 1
            end = command.index("\"--internal\"")
            return " ".join(command[start:end])
    raise Exception("Unable to find meson command from build.ninja")


def generate_guid():
    return str(uuid.uuid4()).upper()


vs_header_tmpl = """<?xml version="1.0" ?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003" DefaultTargets="Build" ToolsVersion="4.0">
\t<ItemGroup Label="ProjectConfigurations">
\t\t<ProjectConfiguration Include="{configuration}|{platform}">
\t\t\t<Configuration>{configuration}</Configuration>
\t\t\t<Platform>{platform}</Platform>
\t\t</ProjectConfiguration>
\t</ItemGroup>\n""".expandtabs(4)

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
\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.Default.props"/>\n""".expandtabs(4)

vs_config_tmpl = """\t<PropertyGroup Label="Configuration">
\t\t<PlatformToolset>v142</PlatformToolset>
\t\t<ConfigurationType>{config_type}</ConfigurationType>
\t</PropertyGroup>
\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.props"/>\n""".expandtabs(4)

vs_propertygrp_tmpl = """\t<PropertyGroup>
\t\t<OutDir>{out_dir}</OutDir>
\t\t<IntDir>{intermediate_dir}</IntDir>
\t\t<TargetName>{output}</TargetName>
\t</PropertyGroup>\n""".expandtabs(4)

vs_nmake_tmpl = """\t<PropertyGroup>
\t\t<NMakeBuildCommandLine>{build_cmd}</NMakeBuildCommandLine>
\t\t<NMakeOutput>{output}</NMakeOutput>
\t\t<NMakeCleanCommandLine>{clean_cmd}</NMakeCleanCommandLine>
\t\t<NMakeReBuildCommandLine>{rebuild_cmd}</NMakeReBuildCommandLine>
\t\t<NMakeIncludeSearchPath>{includes}</NMakeIncludeSearchPath>
\t\t<NMakePreprocessorDefinitions>{preprocessor_macros}</NMakePreprocessorDefinitions>
\t\t<AdditionalOptions>{additional_options}</AdditionalOptions>
\t</PropertyGroup>\n""".expandtabs(4)

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
\t</ItemGroup>\n""".expandtabs(4)

vs_dependency_tmpl = """\t\t<ProjectReference Include="{vcxproj_name}">
\t\t\t<Project>{{{project_guid}}}</Project>
\t\t\t<LinkLibraryDependencies>{link_deps}</LinkLibraryDependencies>
\t\t</ProjectReference>\n""".expandtabs(4)

vs_end_proj_tmpl = """\t\t<Import Project="$(VCTargetsPath)\Microsoft.Cpp.targets"/>
\t<ImportGroup Label="ExtensionTargets"/>
</Project>""".expandtabs(4)


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
        if not(path.exists()):
            raise Exception(f"Introspect data is missing!. Unable to generate Visual Studio solutions.")
        intro[key] = json.load(open(intro[key]))
    return intro


class VisualStudioSolution:
    def __init__(self, build_dir):
        self.build_dir = Path(build_dir)
        if not(Path(build_dir).is_absolute()):
            self.build_dir = self.build_dir.absolute()
        cl_location = subprocess.check_output('where cl')
        arch = os.path.basename(os.path.dirname(cl_location)).decode('utf-8')
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
        self.meson = get_meson_command(self.build_dir / 'build.ninja')
        self.ninja_proj = VcxProj("ninja", "ninja", generate_guid(), True)
        self.vcxprojs.append(self.ninja_proj)
        self.generate_run_proj(self.ninja_proj, f'{self.meson} compile')
        install_proj = VcxProj("RUN_INSTALL", "RUN_INSTALL", generate_guid(), False)
        self.vcxprojs.append(install_proj)
        self.generate_run_proj(install_proj, f'{self.meson} install')
        test_proj = VcxProj("RUN_TESTS", "RUN_TESTS", generate_guid(), False)
        self.vcxprojs.append(test_proj)
        self.generate_run_proj(test_proj, f'{self.meson} test')
        self.generate_regen_proj()
        for target in self.intro['targets']:
            guid = generate_guid()
            vcxproj = VcxProj(target['name'], target['id'], guid, False)
            self.vcxprojs.append(vcxproj)
            if target['type'] == 'run':
                self.generate_run_proj(vcxproj, f'{self.meson} compile {target["name"]}')
            else:
                self.generate_vcxproj(BuildTarget(target, guid))
        self.generate_solution(self.intro['projectinfo']['descriptive_name'] + '.sln')

    def generate_run_proj(self, proj: VcxProj, command):
        proj_file = open(f'{self.build_dir}/{proj.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(configuration=self.build_type,
                                              platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(guid=proj.guid,
                                               platform=self.platform,
                                               name=proj.name))
        proj_file.write(vs_config_tmpl.format(config_type="Utility"))
        proj_file.write(vs_propertygrp_tmpl.format(out_dir='.\\',
                                                   intermediate_dir=f'{proj.name}-temp\\',
                                                   output=f'{proj.name}'))
        proj_contents = self.build_dir / 'meson-private' / f'always_rebuild_{proj.name}.rule'
        proj_output = self.build_dir / 'meson-private' / 'always_rebuild.regen'
        proj_file.write(vs_custom_itemgroup_tmpl.format(
            command=command + " $(LocalDebuggerCommandArguments)",
            out_file=proj_output,
            additional_inputs="",
            contents=str(proj_contents),
            verify_io='False'
        ))
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

        if not(proj_contents.exists()):
            open(proj_contents, 'w', encoding='utf-8').close()

    def generate_regen_proj(self):
        self.regen_proj = VcxProj("REGEN", "REGEN", generate_guid(), True)
        self.vcxprojs.append(self.regen_proj)
        proj_file = open(f'{self.build_dir}/{self.regen_proj.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(
            configuration=self.build_type,
            platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(
            guid=self.regen_proj.guid,
            platform=self.platform,
            name=self.regen_proj.name))
        proj_file.write(vs_config_tmpl.format(config_type="Utility"))
        proj_file.write(vs_propertygrp_tmpl.format(
            out_dir='.\\',
            intermediate_dir='vs-regen-temp\\',
            output='vs_regen'))
        proj_contents = self.build_dir / 'meson-private' / 'vs_regen.rule'
        proj_output = self.build_dir / 'meson-private' / 'vs_regen.out'
        proj_file.write(vs_custom_itemgroup_tmpl.format(
            command=f'{sys.executable} {os.path.abspath(__file__)} --build_root {self.build_dir}',
            additional_inputs=";".join(self.intro['buildsystem_files']),
            out_file=proj_output,
            contents=str(proj_contents),
            verify_io=True))
        proj_file.write('\t<ItemGroup>\n')
        proj_file.write(vs_dependency_tmpl.format(
            vcxproj_name=f'{self.ninja_proj.id}.vcxproj',
            project_guid=self.ninja_proj.guid,
            link_deps='false'))
        proj_file.write('\t</ItemGroup>\n')
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()
        if not(proj_contents.exists()):
            open(proj_contents, 'w', encoding='utf-8').close()
        open(proj_output, 'w', encoding='utf-8').close()

    def generate_vcxproj(self, target: BuildTarget):
        proj_file = open(f'{self.build_dir}/{target.id}.vcxproj', 'w', encoding='utf-8')
        proj_file.write(vs_header_tmpl.format(
            configuration=self.build_type,
            platform=self.platform))
        proj_file.write(vs_globals_tmpl.format(
            guid=target.guid,
            name=target.name,
            platform=self.platform))

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
        proj_file.write(vs_nmake_tmpl.format(
            output=target.output,
            build_cmd=f'{self.meson} compile {target.name}',
            clean_cmd=f'{self.meson} compile --clean',
            rebuild_cmd=f'{self.meson} compile --clean \n {self.meson} compile {target.name}',
            includes=";".join(include_paths),
            preprocessor_macros=";".join(preprocessor_macros),
            additional_options=";".join(additional_options),
        ))

        # Add git tracked headers
        proj_file.write('\t<ItemGroup>\n')
        all_headers = set()
        for src in target.sources + target.extra_files:
            proj_file.write(f'\t\t<ClCompile Include="{src}"/>\n')
            # Find used headers
            includes = ['-I' + inc for inc in include_paths]
            used_headers = subprocess.Popen(['cl', '-nologo', '-showIncludes', '-c', '-Fonul'] + includes + [src],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT).communicate()[0].decode('utf-8')
            for line in used_headers.split('\n'):
                include_prefix = 'Note: including file:'
                if line.startswith(include_prefix):
                    all_headers.add(Path(line[len(include_prefix):].strip()))
        git_tracked = subprocess.check_output(['git', 'ls-files'], cwd=self.source_dir).decode('utf-8').split('\n')
        git_tracked = set([Path(f) for f in git_tracked])
        for header in all_headers:
            try:
                relpath = Path(header).relative_to(self.source_dir)
                if relpath in git_tracked:
                    proj_file.write(f'\t\t<ClCompile Include="{header}"/>\n')
            except ValueError:
                pass
        proj_file.write('\t</ItemGroup>\n')
        proj_file.write(vs_end_proj_tmpl)
        proj_file.close()

    def generate_solution(self, sln_filename):
        sln = open(f'{self.build_dir}/{sln_filename}', 'w', encoding='utf-8')
        sln.write('Microsoft Visual Studio Solution File, Format Version 12.00\n')
        sln.write('# Visual Studio 2019\n')
        project_guid = generate_guid()
        for proj in self.vcxprojs:
            sln.write(f'Project("{{{project_guid}}}") = "{proj.name}", "{proj.id}.vcxproj", "{{{proj.guid}}}"\n')
            sln.write('EndProject\n')
        sln.write('Global\n')
        sln.write('\tGlobalSection(SolutionConfigurationPlatforms) = preSolution\n')
        sln.write(f'\t\t{self.build_type}|{self.platform} = {self.build_type}|{self.platform}\n')
        sln.write('\tEndGlobalSection\n')

        sln.write('\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n')
        for proj in self.vcxprojs:
            sln.write(f'\t\t{{{proj.guid}}}.{self.build_type}|{self.platform}.ActiveCfg = {self.build_type}|{self.platform}\n')
            if proj.build_by_default:
                sln.write(f'\t\t{{{proj.guid}}}.{self.build_type}|{self.platform}.Build.0 = {self.build_type}|{self.platform}\n')
        sln.write('\tEndGlobalSection\n')

        sln.write('\tGlobalSection(SolutionProperties) = preSolution\n')
        sln.write('\t\tHideSolutionNode = FALSE\n')
        sln.write('\tEndGlobalSection\n')
        sln.write('EndGlobal\n')
        sln.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create Visual Studio solution with ninja backend.')
    parser.add_argument('-b', '--build_root', type=str, help='Path to build directory root')
    args = parser.parse_args()

    VisualStudioSolution(args.build_root)
