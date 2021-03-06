# ==================================================================================================
# Copyright 2012 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

import os
import pkgutil

from collections import defaultdict

from twitter.common.collections import OrderedSet, OrderedDict
from twitter.common.dirutil import safe_delete, safe_mkdir, safe_open

from twitter.pants.base.build_environment import get_buildroot
from twitter.pants.base.generator import TemplateData, Generator
from twitter.pants.tasks.ide_gen import IdeGen


_TEMPLATE_BASEDIR = os.path.join('templates', 'eclipse')


_VERSIONS = {
  '3.5': '3.7', # 3.5-3.7 are .project/.classpath compatible
  '3.6': '3.7',
  '3.7': '3.7',
}


_SETTINGS = (
  'org.eclipse.core.resources.prefs',
  'org.eclipse.jdt.ui.prefs',
)


class EclipseGen(IdeGen):
  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    IdeGen.setup_parser(option_group, args, mkflag)

    supported_versions = sorted(list(_VERSIONS.keys()))
    option_group.add_option(mkflag("eclipse-version"), dest="eclipse_gen_version",
                            default='3.6', type="choice", choices=supported_versions,
                            help="[%%default] The Eclipse version the project "
                                   "configuration should be generated for; can be one of: "
                                   "%s" % supported_versions)

  def __init__(self, context):
    IdeGen.__init__(self, context)

    version = _VERSIONS[context.options.eclipse_gen_version]
    self.project_template = os.path.join(_TEMPLATE_BASEDIR, 'project-%s.mustache' % version)
    self.classpath_template = os.path.join(_TEMPLATE_BASEDIR, 'classpath-%s.mustache' % version)
    self.apt_template = os.path.join(_TEMPLATE_BASEDIR, 'factorypath-%s.mustache' % version)
    self.pydev_template = os.path.join(_TEMPLATE_BASEDIR, 'pydevproject-%s.mustache' % version)
    self.debug_template = os.path.join(_TEMPLATE_BASEDIR, 'debug-launcher-%s.mustache' % version)
    self.coreprefs_template = os.path.join(_TEMPLATE_BASEDIR,
                                           'org.eclipse.jdt.core.prefs-%s.mustache' % version)

    self.project_filename = os.path.join(self.cwd, '.project')
    self.classpath_filename = os.path.join(self.cwd, '.classpath')
    self.apt_filename = os.path.join(self.cwd, '.factorypath')
    self.pydev_filename = os.path.join(self.cwd, '.pydevproject')
    self.coreprefs_filename = os.path.join(self.cwd, '.settings', 'org.eclipse.jdt.core.prefs')

  def generate_project(self, project):
    def linked_folder_id(source_set):
      return source_set.source_base.replace(os.path.sep, '.')

    def base_path(source_set):
      return os.path.join(source_set.root_dir, source_set.source_base)

    def create_source_base_template(source_set):
      source_base = base_path(source_set)
      return source_base, TemplateData(
        id=linked_folder_id(source_set),
        path=source_base
      )

    source_bases = dict(map(create_source_base_template, project.sources))
    if project.has_python:
      source_bases.update(map(create_source_base_template, project.py_sources))
      source_bases.update(map(create_source_base_template, project.py_libs))

    def create_source_template(base_id, includes=None, excludes=None):
      return TemplateData(
        base=base_id,
        includes='|'.join(OrderedSet(includes)) if includes else None,
        excludes='|'.join(OrderedSet(excludes)) if excludes else None,
      )

    def create_sourcepath(base_id, sources):
      def normalize_path_pattern(path):
        return '%s/' % path if not path.endswith('/') else path

      includes = [normalize_path_pattern(src_set.path) for src_set in sources if src_set.path]
      excludes = []
      for source_set in sources:
        excludes.extend(normalize_path_pattern(exclude) for exclude in source_set.excludes)

      return create_source_template(base_id, includes, excludes)

    pythonpaths = []
    if project.has_python:
      for source_set in project.py_sources:
        pythonpaths.append(create_source_template(linked_folder_id(source_set)))
      for source_set in project.py_libs:
        lib_path = source_set.path if source_set.path.endswith('.egg') else '%s/' % source_set.path
        pythonpaths.append(create_source_template(linked_folder_id(source_set),
                                                  includes=[lib_path]))

    configured_project = TemplateData(
      name=self.project_name,
      java=TemplateData(
        jdk=self.java_jdk,
        language_level=('1.%d' % self.java_language_level)
      ),
      python=project.has_python,
      scala=project.has_scala and not project.skip_scala,
      source_bases=source_bases.values(),
      pythonpaths=pythonpaths,
      debug_port=project.debug_port,
    )

    outdir = os.path.abspath(os.path.join(self.work_dir, 'bin'))
    safe_mkdir(outdir)

    source_sets = defaultdict(OrderedSet) # base_id -> source_set
    for source_set in project.sources:
      source_sets[linked_folder_id(source_set)].add(source_set)
    sourcepaths = [create_sourcepath(base_id, sources) for base_id, sources in source_sets.items()]

    libs = []
    def add_jarlibs(classpath_entries):
      for classpath_entry in classpath_entries:
        libs.append((classpath_entry.jar, classpath_entry.source_jar))
    add_jarlibs(project.internal_jars)
    add_jarlibs(project.external_jars)

    configured_classpath = TemplateData(
      sourcepaths=sourcepaths,
      has_tests=project.has_tests,
      libs=libs,
      scala=project.has_scala,

      # Eclipse insists the outdir be a relative path unlike other paths
      outdir=os.path.relpath(outdir, get_buildroot()),
    )

    def apply_template(output_path, template_relpath, **template_data):
      with safe_open(output_path, 'w') as output:
        Generator(pkgutil.get_data(__name__, template_relpath), **template_data).write(output)

    apply_template(self.project_filename, self.project_template, project=configured_project)
    apply_template(self.classpath_filename, self.classpath_template, classpath=configured_classpath)
    apply_template(os.path.join(self.work_dir, 'Debug on port %d.launch' % project.debug_port),
                   self.debug_template, project=configured_project)
    apply_template(self.coreprefs_filename, self.coreprefs_template, project=configured_project)

    for resource in _SETTINGS:
      with safe_open(os.path.join(self.cwd, '.settings', resource), 'w') as prefs:
        prefs.write(pkgutil.get_data(__name__, os.path.join('files', 'eclipse', resource)))

    factorypath = TemplateData(
      project_name=self.project_name,

      # The easiest way to make sure eclipse sees all annotation processors is to put all libs on
      # the apt factorypath - this does not seem to hurt eclipse performance in any noticeable way.
      jarpaths=libs
    )
    apply_template(self.apt_filename, self.apt_template, factorypath=factorypath)

    if project.has_python:
      apply_template(self.pydev_filename, self.pydev_template, project=configured_project)
    else:
      safe_delete(self.pydev_filename)

    print('\nGenerated project at %s%s' % (self.work_dir, os.sep))
