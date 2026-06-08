#!/usr/bin/env python3
"""Render Mapper Pro — Deadline plugin.

Runs a Render Mapper Pro job (video mapped onto a Cinema 4D / Redshift scene)
on any render node, regardless of the OS the job was submitted from. The node
locates its own c4dpy, translates the staged file paths to its own repository
mount, feeds c4dpy's license prompt, and runs the bundled worker for this
task's frame range. Carries the app icon (RenderMapperPro.ico) so jobs show it
in the Deadline Monitor.

Submitted with:
  Plugin=RenderMapperPro
  plugin_info: SubmitRepoRoot, ConfigFile (repo-relative), WorkerScript
               (repo-relative), C4DPyExecutable (optional override)
"""
from __future__ import absolute_import

import glob
import json
import os
import sys

from Deadline.Plugins import DeadlinePlugin, PluginType
from Deadline.Scripting import RepositoryUtils


def GetDeadlinePlugin():
    return RenderMapperProPlugin()


def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()


class RenderMapperProPlugin(DeadlinePlugin):
    def __init__(self):
        if sys.version_info.major == 3:
            super().__init__()
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderTasksCallback += self.RenderTasks

    def Cleanup(self):
        del self.InitializeProcessCallback
        del self.RenderTasksCallback

    def InitializeProcess(self):
        self.SingleFramesOnly = False
        self.PluginType = PluginType.Advanced

    # ── helpers ──────────────────────────────────────────────────────────
    def _join_repo(self, repo, rel):
        rel = rel.replace("\\", "/").lstrip("/")
        return os.path.join(repo, *rel.split("/")) if rel else ""

    def _translate(self, path, submit_repo, node_repo):
        """Map an absolute path written against the submitting machine's repo
        root onto this node's repo root (and apply any configured Deadline path
        mapping)."""
        if not path:
            return path
        mapped = RepositoryUtils.CheckPathMapping(path)
        if submit_repo:
            a = mapped.replace("\\", "/")
            b = submit_repo.replace("\\", "/").rstrip("/")
            if a.lower().startswith(b.lower()):
                rel = a[len(b):].lstrip("/")
                return os.path.join(node_repo, *rel.split("/"))
        return mapped

    def _find_c4dpy(self):
        if os.name == "nt":
            pats = [r"C:\Program Files\Maxon Cinema 4D *\c4dpy.exe",
                    r"C:\Program Files\Maxon\Cinema 4D *\c4dpy.exe",
                    r"C:\Maxon\Cinema 4D *\c4dpy.exe"]
        elif sys.platform == "darwin":
            pats = ["/Applications/Maxon Cinema 4D */c4dpy.app/Contents/MacOS/c4dpy"]
        else:
            pats = ["/opt/maxon/cinema4d*/bin/c4dpy", "/opt/Maxon*/c4dpy",
                    "/usr/local/Maxon*/c4dpy"]
        found = []
        for p in pats:
            found += glob.glob(p)
        found.sort()
        return found[-1] if found else ""

    def _produced(self, out_path, start, cfg):
        if not out_path:
            return False
        fmt = str(cfg.get("render", {}).get("output_format", "")).upper()
        if fmt in ("MPEG4", "QUICKTIME"):
            return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        return os.path.isfile(os.path.join(out_path, "%04d.png" % start))

    # ── render ───────────────────────────────────────────────────────────
    def RenderTasks(self):
        node_repo = RepositoryUtils.GetRootDirectory()
        submit_repo = self.GetPluginInfoEntryWithDefault("SubmitRepoRoot", "").strip()
        cfg_rel = self.GetPluginInfoEntryWithDefault("ConfigFile", "").strip()
        worker_rel = self.GetPluginInfoEntryWithDefault("WorkerScript", "").strip()
        c4dpy_override = self.GetPluginInfoEntryWithDefault("C4DPyExecutable", "").strip()
        jobs = self.GetJobsDataDirectory()

        cfg_path = self._join_repo(node_repo, cfg_rel)
        worker = self._join_repo(node_repo, worker_rel)
        self.LogInfo("RenderMapperPro: repo=%s" % node_repo)
        self.LogInfo("RenderMapperPro: config=%s" % cfg_path)
        self.LogInfo("RenderMapperPro: worker=%s" % worker)

        with open(cfg_path, "r") as fh:
            cfg = json.load(fh)

        cfg["scene_path"] = self._translate(cfg.get("scene_path", ""), submit_repo, node_repo)
        if cfg.get("video_path"):
            cfg["video_path"] = self._translate(cfg["video_path"], submit_repo, node_repo)
        for a in cfg.get("material_assignments", []):
            if a.get("video_path"):
                a["video_path"] = self._translate(a["video_path"], submit_repo, node_repo)
        out_path = self._translate(cfg.get("output_path", ""), submit_repo, node_repo)
        cfg["output_path"] = out_path
        cfg["ffmpeg_path"] = ""  # use the node's PATH ffmpeg

        node_cfg = os.path.join(jobs, "rmp_config.json")
        with open(node_cfg, "w") as fh:
            json.dump(cfg, fh)

        c4dpy = c4dpy_override or self._find_c4dpy()
        if not c4dpy:
            self.FailRender("RenderMapperPro: could not locate c4dpy on this node. "
                            "Set 'C4DPyExecutable' in the plugin info to override.")
            return

        start, end = self.GetStartFrame(), self.GetEndFrame()
        # Render log lives in the repo staging dir (next to the config) so it is
        # visible off-node for diagnosis, not just on the worker's local disk.
        logf = os.path.join(os.path.dirname(cfg_path), "rmp_render.log")
        node = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "?"
        try:
            with open(logf, "w") as fh:
                fh.write("RenderMapperPro node=%s os=%s c4dpy=%s worker=%s frames=%d-%d\n"
                         % (node, os.name, c4dpy, worker, start, end))
        except Exception:
            pass
        if os.name == "nt":
            wrapper = os.path.join(jobs, "rmp_run.bat")
            with open(wrapper, "w") as fh:
                fh.write("@echo off\r\n")
                fh.write('echo 1| "%s" "%s" "%s" %d %d >> "%s" 2>&1\r\n'
                         % (c4dpy, worker, node_cfg, start, end, logf))
            shell = os.environ.get("COMSPEC", "cmd.exe")
            args = '/c "%s"' % wrapper
        else:
            wrapper = os.path.join(jobs, "rmp_run.sh")
            with open(wrapper, "w") as fh:
                fh.write("#!/bin/sh\n")
                fh.write("printf '1\\n' | \"%s\" \"%s\" \"%s\" %d %d >> \"%s\" 2>&1\n"
                         % (c4dpy, worker, node_cfg, start, end, logf))
            try:
                os.chmod(wrapper, 0o755)
            except OSError:
                pass
            shell = "/bin/sh"
            args = '"%s"' % wrapper

        self.LogInfo("RenderMapperPro: c4dpy=%s" % c4dpy)
        self.LogInfo("RenderMapperPro: rendering frames %d-%d" % (start, end))
        exit_code = self.RunProcess(shell, args, jobs, -1)

        # Surface the worker's own log into the Deadline task report.
        try:
            with open(logf, "r") as fh:
                for line in fh:
                    self.LogInfo(line.rstrip())
        except Exception:
            pass

        # c4dpy frequently throws on interpreter teardown after a good render,
        # so success is judged by produced output rather than the exit code.
        if self._produced(out_path, start, cfg):
            self.LogInfo("RenderMapperPro: output present — task succeeded.")
            return
        self.FailRender("RenderMapperPro: no output produced (c4dpy exit code %s). "
                        "See log above." % exit_code)
