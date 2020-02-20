#!/usr/bin/env python3
#
# Copyright 2018 The Bazel Authors. All rights reserved.
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

import sys
import datetime
import json
import threading

import bazelci

BUILDKITE_ORG = "bazel"
DOWNSTREAM_PIPELINE = "bazel-at-head-plus-downstream"
CULPRIT_FINDER_PIPELINE = "culprit-finder"

CACHE = False

COLORS = {
    "SERIOUS" : '\033[95m',
    "INFO" : '\033[94m',
    "PASSED" : '\033[92m',
    "WARNING" : '\033[93m',
    "FAIL" : '\033[91m',
    "ENDC" : '\033[0m',
    "BOLD" : '\033[1m',
}

# Create a BuildKiteClient for a given pipeline
def get_new_buildkite_client(pipeline):
    return bazelci.BuildkiteClient(BUILDKITE_ORG, pipeline)

DOWNSTREAM_PIPELINE_CLIENT = get_new_buildkite_client(DOWNSTREAM_PIPELINE)
CULPRIT_FINDER_PIPELINE_CLIENT = get_new_buildkite_client(CULPRIT_FINDER_PIPELINE)

def print_info(context, style, info):
    info_str = "\n".join(info)
    bazelci.execute_command(
        [
            "buildkite-agent",
            "annotate",
            "--append",
            f"--context={context}",
            f"--style={style}",
            f"\n{info_str}\n",
        ]
    )

class BuildInfoAnalyzer(threading.Thread):

    def __init__(self, project, pipeline, downstream_result):
        threading.Thread.__init__(self)
        self.project = project
        self.pipeline = pipeline
        self.downstream_result = downstream_result
        self.main_result = None
        self.client = get_new_buildkite_client(pipeline)
        # Set default analyze result
        self.analyze_result = {
            "result": "passed",
            "main_fail_reason": None,
            "downstream_fail_reason": None,
            "bisect_result": None,
        }


    def __get_main_build_result(self):
        a_day_ago = datetime.datetime.utcnow() - datetime.timedelta(days = 1)
        build_info_list = self.client.get_build_info_list([
            ("branch", "master"),
            ("created_from", a_day_ago.isoformat()),
            ("state[]", "failed"),
            ("state[]", "passed"),
        ])
        if len(build_info_list) == 0:
            raise bazelci.BuildkiteException("Cannot find finished build in the past day for pipeline %s, please try to rerun the pipeline first." % self.pipeline)
        main_build_info = build_info_list[0]

        self.main_result = {}
        self.main_result["commit"] = main_build_info["commit"]
        self.main_result["build_number"] = main_build_info["number"]
        job_infos = filter(lambda x: x != None, [extract_job_info_by_key(job) for job in main_build_info["jobs"]])
        self.main_result["tasks"] = group_job_info_by_task(job_infos)
        self.main_result["state"] = get_project_state(self.main_result["tasks"])

        last_green_commit_url = bazelci.bazelci_last_green_commit_url(
            bazelci.DOWNSTREAM_PROJECTS[self.project]["git_repository"], self.pipeline
        )
        self.main_result["last_green_commit"] = bazelci.get_last_green_commit(last_green_commit_url)


    def __log(self, c, text):
        info = [
            "```term",
            COLORS["INFO"] + f"Analyzing {self.project}: " + COLORS["ENDC"] + COLORS[c] + text + COLORS["ENDC"],
            "```",
        ]
        print_info(self.pipeline, "info", info)


    def __trigger_bisect(self, tasks):
        env = {
            "PROJECT_NAME": self.project,
            "TASK_NAME_LIST": ",".join(tasks) if tasks else "",
        }
        return CULPRIT_FINDER_PIPELINE_CLIENT.trigger_new_build("HEAD", f"Bisecting {self.project}", env)


    def __determine_bisect_result(self, job):
        bisect_log = CULPRIT_FINDER_PIPELINE_CLIENT.get_build_log(job)
        pos = bisect_log.rfind("first bad commit is")
        if pos != -1:
            return "\n".join([
                 "Culprit found:",
                 bisect_log[pos:],
                 "Bisect URL: " + job["web_url"],
            ])
        pos = bisect_log.rfind("Given good commit")  # Matching "Given good commit (XXXX) is not actually good, abort bisecting."
        if pos != -1:
            return "\n".join([
                "Given good commit is now failing. This is probably caused remote cache issue or infra change.",
                "Please ping philwo@ or pcloudy@ for investigation.",
                "Bisect URL: " + job["web_url"],
            ])
        pos = bisect_log.rfind("first bad commit not found, every commit succeeded.")
        if pos != -1:
            return "\n".join([
                "Bisect didn't manage to reproduce the failure, all builds succeeded.",
                "Maybe the failed build are cached from previous build with a different Bazel version or it could be flaky.",
                "Please try to rerun the bisect with NEEDS_CLEAN=1 and REPEAT_TIMES=3.",
                "Bisect URL: " + job["web_url"],
            ])
        return "Bisect failed due to unknown reason, please check " + job["web_url"]


    def __analyze_result_for_main_pipeline(self):
        failing_tasks_str = ", ".join([task for task, info in self.main_result["tasks"].items() if info["state"] == "failed"])
        last_green_commit = self.main_result["last_green_commit"]
        self.analyze_result["main_fail_reason"] = f"The project is probably broken due to its own change, last_green_commit is {last_green_commit}. Failing tasks are {failing_tasks_str}"


    def __retry_failed_downstream_jobs(self):
        retry_per_failed_task = {}
        for task, info in self.downstream_result["tasks"].items():
            if info["state"] == "failed":
                retry_per_failed_task[task] = DOWNSTREAM_PIPELINE_CLIENT.trigger_job_retry(self.downstream_result["build_number"], info["id"])
        for task, job_info in retry_per_failed_task.items():
            retry_per_failed_task[task] = DOWNSTREAM_PIPELINE_CLIENT.wait_job_to_finish(self.downstream_result["build_number"], job_info["id"])
        return retry_per_failed_task


    def __analyze_result_for_downstream_pipeline(self):
        self.__log("INFO", "Retrying failed downstream tasks...")
        retry_per_failed_task = self.__retry_failed_downstream_jobs()

        # Report succeeded tasks as flaky
        flaky_tasks = [task for task, info in retry_per_failed_task.items() if info["state"] == "passed"]
        if flaky_tasks:
            self.__log("WARNING", "The following tasks passed after retry:")
            for task in flaky_tasks:
                self.__log("WARNING", f"    {task}")

        failing_tasks = [task for task, info in retry_per_failed_task.items() if info["state"] == "failed"]
        bisect_build = self.__trigger_bisect(failing_tasks)
        bisect_build = CULPRIT_FINDER_PIPELINE_CLIENT.wait_build_to_finish(bisect_build["number"])
        bisect_result_by_task = {}
        for task in failing_tasks:
            for job in bisect_build["jobs"]:
                if ("--task_name=" + task) in job["command"]:
                    bisect_result_by_task[task] = self.__determine_bisect_result(job)
            if task not in bisect_result_by_task:
                raise bazelci.BuildkiteException(f"Bisect job for task {task} is missing in " + job["web_url"])
        self.analyze_result["bisect_result"] = bisect_result_by_task


    def analyze(self):
        # Main build: PASSED; Downstream build: PASSED
        if self.main_result["state"] == "passed" and self.downstream_result["state"] == "passed":
            self.__log("PASSED", f"Project passed in both main build and downstream build.")
            return

        # Main build: FAILED; Downstream build: PASSED
        if self.main_result["state"] == "failed" and self.downstream_result["state"] == "passed":
            self.__log("WARNING", "Project passed in downstream build, but failed in main build. The build seems to be broken by changes from the project itself.")
            self.analyze_result["result"] = "failed"
            self.__analyze_result_for_main_pipeline()
            return

        # Main build: PASSED; Downstream build: FAILED
        if self.main_result["state"] == "passed" and self.downstream_result["state"] == "failed":
            self.__log("FAIL", "Project passed in main build, but failed in downstream build. It's likely a Bazel bug.")
            self.__log("FAIL", f"Initial a bisect for {self.project}...")
            self.analyze_result["result"] = "failed"
            self.__analyze_result_for_downstream_pipeline()
            return

        # Main build: FAILED; Downstream build: FAILED
        if self.main_result["state"] == "failed" and self.downstream_result["state"] == "failed":
            self.__log("SERIOUS", f"Project failed in both main build and downstream build. This might be caused by an infra change.")
            last_green_commit = self.main_result["last_green_commit"]
            self.__log("SERIOUS", f"Initial a new build at last green commit {last_green_commit}...")
            build_info = self.client.trigger_new_build(last_green_commit)
            build_info = self.client.wait_build_to_finish(build_info["number"])
            if build_info["state"] == "failed":
                self.__log("SERIOUS", f"Project failed at last green commit. This is probably caused by an infra change, please ping philwo@ or pcloudy@.")
            else:
                self.__log("PASSED", f"Project succeeded at last green commit.")
                self.__analyze_result_for_main_pipeline()
                self.__analyze_result_for_downstream_pipeline()
            return


    def report(self):
        pass


    def run(self):
        self.__get_main_build_result()
        self.analyze()
        self.report()


# Get the raw downstream build result from the lastest build
def get_latest_downstream_build_info():
    a_day_ago = datetime.datetime.utcnow() - datetime.timedelta(days = 1)
    downstream_build_list = DOWNSTREAM_PIPELINE_CLIENT.get_build_info_list([
        ("branch", "master"),
        ("created_from", a_day_ago.isoformat()),
        ("state[]", "failed"),
        ("state[]", "passed"),
    ])

    if len(downstream_build_list) == 0:
        raise bazelci.BuildkiteException("Cannot find finished downstream build in the past day, please try to rerun downstream pipeline first.")
    return downstream_build_list[0]


# Parse infos from command and extract info from original job infos.
# Result is like:
#     [
#         {"task": "A", "state": "passed", "web_url": "http://foo/bar/A"},
#         {"task": "B", "state": "failed", "web_url": "http://foo/bar/B"},
#     ]
def extract_job_info_by_key(job, info_from_command = [], info_from_job = ["name", "state", "web_url", "exit_status", "id"]):
    if "command" not in job or not job["command"] or "bazelci.py runner" not in job["command"]:
        return None

    # We have to know which task this job info belongs to
    if "task" not in info_from_command:
        info_from_command.append("task")

    job_info = {}

    # Assume there is no space in each argument
    args = job["command"].split(" ")
    for info in info_from_command:
        for arg in args:
            prefix = "--" + info + "="
            if arg.startswith(prefix):
                job_info[info] = arg[len(prefix):]
        if info not in job_info:
            return None

    for info in info_from_job:
        if info not in job:
            return None
        job_info[info] = job[info]

    return job_info


# Turn a list of job infos
#     [
#         {"task": "windows", "state": "passed", "web_url": "http://foo/bar/A"},
#         {"task": "macos", "state": "failed", "web_url": "http://foo/bar/B"},
#     ]
# into a map of task name to job info
#     {
#         "windows": {"state": "passed", "web_url": "http://foo/bar/A"},
#         "macos": {"state": "failed", "web_url": "http://foo/bar/B"},
#     }
def group_job_info_by_task(job_infos):
    job_info_by_task = {}
    for job_info in job_infos:
        if "task" not in job_info:
            raise bazelci.BuildkiteException(f"'task' must be a key of job_info: {job_info}")

        task_name = job_info["task"]
        del job_info["task"]
        job_info_by_task[task_name] = job_info

    return job_info_by_task


def get_project_state(tasks):
    # If any of the jobs didn't pass, we condsider the state of this project failed.
    for _, infos in tasks.items():
        if infos["state"] != "passed":
            return "failed"
    return "passed"


# Get the downstream build result in a structure like:
# {
#     "project_x" : {
#         "commit": "XXXXXXXX",
#         "bazel_commit": "XXXXXXXX",
#         "tasks" : {
#             "A": {"state": "passed", "web_url": "http://foo/bar/A"},
#             "B": {"state": "failed", "web_url": "http://foo/bar/B"},
#         }
#     }
#     "project_y" : {
#         "commit": "XXXXXXXX",
#         "bazel_commit": "XXXXXXXX",
#         "tasks" : {
#             "C": {"state": "passed", "web_url": "http://foo/bar/C"},
#             "D": {"state": "failed", "web_url": "http://foo/bar/D"},
#         }
#     }
# }
def get_downstream_result_by_project(downstream_build_info):
    config_to_project = {}
    for project_name, project_info in bazelci.DOWNSTREAM_PROJECTS_PRODUCTION.items():
        config_to_project[project_info["http_config"]] = project_name

    downstream_result = {}
    jobs_per_project = {}

    for job in downstream_build_info["jobs"]:
        job_info = extract_job_info_by_key(job = job, info_from_command = ["http_config", "git_commit"])
        if job_info:
            project_name = config_to_project[job_info["http_config"]]
            if project_name not in downstream_result:
                jobs_per_project[project_name] = []
                downstream_result[project_name] = {}
                downstream_result[project_name]["bazel_commit"] = downstream_build_info["commit"]
                downstream_result[project_name]["build_number"] = downstream_build_info["number"]
                downstream_result[project_name]["commit"] = job_info["git_commit"]
            jobs_per_project[project_name].append(job_info)

    for project_name in jobs_per_project:
        tasks = group_job_info_by_task(jobs_per_project[project_name])
        downstream_result[project_name]["tasks"] = tasks
        downstream_result[project_name]["state"] = get_project_state(tasks)

    return downstream_result


def main(argv=None):
    downstream_build_info = get_latest_downstream_build_info()
    downstream_result = get_downstream_result_by_project(downstream_build_info)

    analyzers = []
    for project_name, project_info in bazelci.DOWNSTREAM_PROJECTS.items():
        if "disabled_reason" not in project_info:
            analyzer = BuildInfoAnalyzer(project_name, project_info["pipeline_slug"], downstream_result[project_name])
            analyzers.append(analyzer)
            analyzer.start()

    for analyzer in analyzers:
        analyzer.join()
    return 0

if __name__ == "__main__":
    sys.exit(main())
