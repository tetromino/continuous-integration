# Copyright 2015 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Some definition to setup jenkins and build the corresponding docker images

load("@bazel_tools//tools/build_defs/docker:docker.bzl", "docker_build")
load("@bazel_tools//tools/build_defs/pkg:pkg.bzl", "pkg_tar")
load(":plugins.bzl", "JENKINS_PLUGINS", "JENKINS_PLUGINS_VERSIONS")

JENKINS_PORT = 80

JENKINS_HOST = "jenkins"

MAILS_SUBSTITUTIONS = {
    "%{BAZEL_BUILD_RECIPIENT}": "bazel-ci@googlegroups.com",
    "%{BAZEL_RELEASE_RECIPIENT}": "bazel-discuss+release@googlegroups.com",
    "%{SENDER_EMAIL}": "noreply@bazel.io",
}

def _xml_escape(s):
  """Replace XML special characters."""
  s = s.replace("&", "&amp;").replace("'", "&apos;").replace('"', "&quot;")
  s = s.replace("<", "&lt;").replace(">", "&gt;")
  return s

def expand_template_impl(ctx):
  """Simply spawm the template_action in a rule."""
  ctx.template_action(
      template = ctx.file.template,
      output = ctx.outputs.out,
      substitutions = ctx.attr.substitutions,
      executable = ctx.attr.executable,
      )

expand_template = rule(
    attrs = {
        "template": attr.label(
            mandatory = True,
            allow_files = True,
            single_file = True,
        ),
        "substitutions": attr.string_dict(mandatory = True),
        "out": attr.output(mandatory = True),
        "executable": attr.bool(default = True),
    },
    implementation = expand_template_impl,
)

def merge_jobs_impl(ctx):
  """Merge a list of jenkins jobs in a tar ball with the correct layout."""
  output = ctx.outputs.out
  build_tar = ctx.executable._build_tar
  args = [
      "--output=" + output.path,
      "--directory=" + ctx.attr.directory,
      "--mode=0644",
      ]
  args += ["--file=%s=jobs/%s/config.xml" % (f.path, f.basename[:-4])
           for f in ctx.files.srcs]
  ctx.action(
      executable = build_tar,
      arguments = args,
      inputs = ctx.files.srcs,
      outputs = [output],
      mnemonic="MergeJobs"
      )

merge_jobs = rule(
    attrs = {
        "srcs": attr.label_list(allow_files=FileType([".xml"])),
        "directory": attr.string(default="/"),
        "_build_tar": attr.label(
            default=Label("@bazel_tools//tools/build_defs/pkg:build_tar"),
            cfg=HOST_CFG,
            executable=True,
            allow_files=True),
    },
    outputs = {"out": "%{name}.tar"},
    implementation = merge_jobs_impl,
)

def jenkins_job(name, config, substitutions = {},
                project='bazel', org='bazelbuild', project_url=None,
                platforms=[]):
  """Create a job configuration on Jenkins."""
  if not project_url:
    project_url = "https://github.com/%s/%s" % (org, project.lower())
  substitutions = substitutions + JENKINS_PLUGINS_VERSIONS + {
      "%{GITHUB_URL}": "https://github.com/%s/%s" % (org, project.lower()),
      "%{GITHUB_PROJECT}": "%s/%s" % (org, project.lower()),
      "%{PROJECT_URL}": project_url,
      "%{PLATFORMS}": "".join(["<string>%s</string>" % p for p in platforms]),
      } + MAILS_SUBSTITUTIONS
  expand_template(
      name = name,
      template = config,
      out = "%s.xml" % name,
      substitutions = JENKINS_PLUGINS_VERSIONS + substitutions,
    )

def bazel_github_job(name, platforms=[], branch="master", project=None, org="google",
                     project_url=None, workspace=".", configure=[],
                     tests=["//..."], targets=["//..."], substitutions={},
                     test_opts=["--test_output=errors", "--test_tag_filters -noci"],
                     build_opts=["--verbose_failures"]):
  """Create a generic github job configuration to build against Bazel head."""
  if not project:
    project = name
  substitutions = substitutions + {
    "%{WORKSPACE}": workspace,
    "%{PROJECT_NAME}": project,
    "%{BRANCH}": branch,
    "%{CONFIGURE}": _xml_escape("\n".join(configure)),
    "%{TEST_OPTS}": _xml_escape(" ".join(test_opts)),
    "%{BUILD_OPTS}":_xml_escape(" ".join(build_opts)),
    "%{TESTS}": _xml_escape(" ".join(tests)),
    "%{BUILDS}": _xml_escape(" ".join(targets))
  }

  jenkins_job(
      name = name,
      config = "//jenkins:github-jobs.xml.tpl",
      substitutions=substitutions,
      project=project,
      org=org,
      project_url=project_url,
      platforms=platforms)

def jenkins_node(name, remote_fs = "/home/ci", num_executors = 1,
                 labels = [], base = None, preference = 1, visibility=None):
  """Create a node configuration on Jenkins, with possible docker image."""
  native.genrule(
      name = name,
      cmd = """cat >$@ <<'EOF'
<?xml version='1.0' encoding='UTF-8'?>
<slave>
  <name>%s</name>
  <description></description>
  <remoteFS>%s</remoteFS>
  <numExecutors>%s</numExecutors>
  <mode>NORMAL</mode>
  <retentionStrategy class="hudson.slaves.RetentionStrategy$$Always"/>
  <launcher class="hudson.slaves.JNLPLauncher"/>
  <label>%s</label>
  <nodeProperties>
    <jp.ikedam.jenkins.plugins.scoringloadbalancer.preferences.BuildPreferenceNodeProperty plugin="scoring-load-balancer@1.0.1">
      <preference>%s</preference>
    </jp.ikedam.jenkins.plugins.scoringloadbalancer.preferences.BuildPreferenceNodeProperty>
  </nodeProperties>
</slave>
EOF
""" % (name, remote_fs, num_executors, " ".join([name] + labels), preference),
      outs = ["nodes/%s/config.xml" % name],
      visibility = visibility,
      )
  if base:
    # Generate docker image startup script
    expand_template(
        name = name + ".docker-launcher",
        out = name + ".docker-launcher.sh",
        template = "slave_setup.sh",
        substitutions = {
            "%{NODE_NAME}": name,
            "%{HOME_FS}": remote_fs,
            "%{JENKINS_SERVER}": "http://%s:%s" % (JENKINS_HOST, JENKINS_PORT),
            },
        executable = True,
        )
    # Generate docker image
    docker_build(
        name = name + ".docker",
        base = base,
        volumes = [remote_fs],
        files = [":%s.docker-launcher.sh" % name],
        data_path = ".",
        entrypoint = [
            "/bin/bash",
            "/%s.docker-launcher.sh" % name,
        ],
        visibility = visibility,
        )

def _basename(f):
  i1 = f.rfind(":")
  i2 = f.rfind("/")
  if i1 > i2:
    f = f[i1:]
  elif i2 > 0:
    f = f[i2:]
  idx = f.rfind(".")
  if idx > 0:
    return f[:idx]
  return f

def _expand_configs(configs, substitutions):
  """Expand tpl files and create them with the good path, stripping config/."""
  confs = []
  for conf in configs:
    ext = conf.rsplit(".", 1)
    if len(ext) == 2 and ext[1] == 'tpl':
      out = ext[0][7:] if ext[0].startswith("config/") else ext[0]
      expand_template(
          name = conf + "-template",
          out = out,
          template = conf,
          substitutions = substitutions,
      )
      confs += [out]
    elif conf.startswith("config/"):
      out = conf[7:]
      native.genrule(
          name = conf + "-template",
          outs = [out],
          srcs = [conf],
          cmd = "cp $< $@",
      )
      confs += [out]
    else:
      confs += [conf]
  return confs

def jenkins_build(name, plugins = None, base = "jenkins-base.tar", configs = [],
                  jobs = [], substitutions = {}, visibility = None):
  """Build the docker image for the Jenkins instance."""
  if not plugins:
    plugins = [p[0] for p in JENKINS_PLUGINS]
  substitutions = substitutions + MAILS_SUBSTITUTIONS
  ### ADD JENKINS PLUGINS ###
  # TODO(dmarting): combine it with remote repositories.
  # TODO(dmarting): maybe we should make that possible from the docker rules
  # directly?
  [native.genrule(
      name = "%s-plugin-%s-rename" % (name, plugin),
      srcs = ["@jenkins_plugin_%s//file" % plugin.replace("-", "_")],
      cmd = "cp $< $@",
      outs = ["%s-%s.jpi" % (name, plugin)],
  ) for plugin in plugins]
  docker_build(
      name = "%s-plugins-base" % name,
      base = "%s-docker-base" % name,
      files = [":%s-%s.jpi" % (name, plugin) for plugin in plugins],
      data_path = ".",
      directory = "/usr/share/jenkins/ref/plugins"
  )
  # We ovewrite jenkins.sh because configuration files are to be replaced,
  # they are not a "reference setup".
  docker_build(
      name = "%s-jenkins-base" % name,
      base = "@jenkins//:image",
      files = ["jenkins.sh"],
      entrypoint = [
          "/bin/bash",
          "/usr/local/bin/jenkins.sh",
      ],
      data_path = ".",
      volumes = ["/opt/secrets"],
      directory = "/usr/local/bin",
  )
  # Expands .tpl files
  confs = _expand_configs(configs, substitutions)

  # Create the structures for jobs
  merge_jobs(
      name = "%s-jobs" % name,
      srcs = jobs,
      directory = "/",
  )
  ### FINAL IMAGE ###
  docker_build(
      name = name,
      files = confs,
      tars = [":%s-jobs" % name],
      data_path = ".",
      base = "%s-jenkins-base" % name,
      directory = "/usr/share/jenkins/ref",
      visibility = visibility,
  )
