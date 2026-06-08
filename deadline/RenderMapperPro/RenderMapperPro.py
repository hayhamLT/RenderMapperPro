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

    def _find_commandline(self):
        """Locate the licensed Cinema 4D Commandline renderer for this node OS."""
        if os.name == "nt":
            pats = [r"C:\Program Files\Maxon Cinema 4D *\Commandline.exe",
                    r"C:\Program Files\Maxon\Cinema 4D *\Commandline.exe",
                    r"C:\Maxon\Cinema 4D *\Commandline.exe"]
        elif sys.platform == "darwin":
            pats = ["/Applications/Maxon Cinema 4D */Commandline.app/Contents/MacOS/Commandline"]
        else:
            pats = ["/opt/maxon/cinema4d*/bin/Commandline", "/opt/Maxon*/Commandline"]
        found = []
        for p in pats:
            found += glob.glob(p)
        found.sort()
        return found[-1] if found else ""

    def _produced(self, out_dir, prefix, start):
        if not out_dir:
            return False
        try:
            for f in os.listdir(out_dir):
                if f.startswith(prefix) and ("%04d" % start) in f and os.path.getsize(os.path.join(out_dir, f)) > 0:
                    return True
        except Exception:
            pass
        return False

    # ── render ───────────────────────────────────────────────────────────
    def RenderTasks(self):
        node_repo = RepositoryUtils.GetRootDirectory()
        submit_repo = self.GetPluginInfoEntryWithDefault("SubmitRepoRoot", "").strip()
        scene_rel = self.GetPluginInfoEntryWithDefault("SceneFile", "").strip()
        out_dir_in = self.GetPluginInfoEntryWithDefault("OutputDirectory", "").strip()
        out_prefix = self.GetPluginInfoEntryWithDefault("OutputPrefix", "frame").strip()
        cmdl_override = self.GetPluginInfoEntryWithDefault("CommandlineExecutable", "").strip()

        scene = self._join_repo(node_repo, scene_rel)
        out_dir = self._translate(out_dir_in, submit_repo, node_repo)
        try:
            if out_dir and not os.path.isdir(out_dir):
                os.makedirs(out_dir)
        except Exception:
            pass

        exe = cmdl_override or self._find_commandline()
        self.LogInfo("RenderMapperPro: repo=%s" % node_repo)
        self.LogInfo("RenderMapperPro: scene=%s" % scene)
        self.LogInfo("RenderMapperPro: output=%s prefix=%s" % (out_dir, out_prefix))
        self.LogInfo("RenderMapperPro: Commandline=%s" % exe)
        if not exe:
            self.FailRender("RenderMapperPro: could not locate the Cinema 4D Commandline "
                            "renderer on this node. Set 'CommandlineExecutable' to override.")
            return
        if not os.path.isfile(scene):
            self.FailRender("RenderMapperPro: prepared scene not found: %s" % scene)
            return

        start, end = self.GetStartFrame(), self.GetEndFrame()
        out_image = os.path.join(out_dir, out_prefix) if out_dir else out_prefix
        # The licensed Cinema 4D Commandline renderer — same engine the stock
        # Cinema4D plugin uses, so node licensing 'just works'. The clip is
        # already baked into the scene as an image sequence.
        args = '-nogui -render "%s" -frame %d %d -oimage "%s" -oformat PNG' % (
            scene, start, end, out_image)
        self.LogInfo("RenderMapperPro: rendering frames %d-%d" % (start, end))
        exit_code = self.RunProcess(exe, args, os.path.dirname(scene), -1)
        self.LogInfo("RenderMapperPro: Commandline exit code %s" % exit_code)

        if self._produced(out_dir, out_prefix, start):
            self.LogInfo("RenderMapperPro: output present — task succeeded.")
            return
        self.FailRender("RenderMapperPro: no output produced (Commandline exit %s)." % exit_code)
