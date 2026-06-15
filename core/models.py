from __future__ import annotations

from dataclasses import asdict, dataclass, field

VIDEO_MAPPING_MODE_EMISSION = "EMISSION_FULL_BRIGHT"
VIDEO_MAPPING_MODE_BASE_COLOR = "BASE_COLOR_ALPHA"


@dataclass
class RenderOptions:
    width: int
    height: int
    fps: int
    frame_start: int
    frame_end: int
    engine: str = "CYCLES"
    samples: int = 64
    use_denoise: bool = True
    frame_step: int = 1
    output_format: str = "MPEG4"
    codec: str = "H264"
    color_view_transform: str = "Filmic"
    color_look: str = "None"
    color_exposure: float = 0.0
    color_gamma: float = 1.0
    timeout_seconds: int = 0
    idle_timeout_seconds: int = 0
    # Performance / quality
    device: str = "AUTO"                 # AUTO | CPU | GPU
    resolution_percentage: int = 100     # render scale %
    film_transparent: bool = False       # transparent background (alpha)
    burn_in: bool = False                # stamp clip/version/frame/date onto frames
    video_quality: str = "HIGH"          # FFMPEG constant_rate_factor
    video_codec: str = ""                # optional codec override (e.g. H265); blank = profile default
    # Redshift optimisation (render-speed levers; honoured only on the C4D path)
    rs_min_samples: int = 4              # unified min samples
    rs_threshold: float = 0.01           # adaptive error threshold — higher = faster/noisier
    rs_gi_enabled: bool = True           # global illumination on/off (off is much faster)
    rs_gi_bounces: int = 3               # GI bounce count — fewer = faster
    rs_ray_depth: int = 6                # combined max trace depth — fewer = faster
    # Web / three.js scene lighting (honoured only on the .glb/.gltf web path)
    web_lighting_preset: str = "auto"        # auto | studio | outdoor | flat | none
    web_lighting_intensity: float = 1.0      # 0.0–2.0 dimmer on env + light rig
    web_respect_scene_lights: bool = True    # skip the rig if the .glb ships its own lights


@dataclass
class MaterialVideoAssignment:
    material_name: str
    video_path: str
    mapping_mode: str = VIDEO_MAPPING_MODE_EMISSION


@dataclass(kw_only=True)
class DeadlineFields:
    """Deadline farm-submission settings shared by JobConfig and RenderJob.

    A single source of truth for the deadline_* fields both carry. Declared
    ``kw_only`` so the flat field API is preserved (``job.deadline_pool``,
    ``asdict``, ``getattr`` all keep working) without disturbing the positional
    construction of the classes that inherit it.
    """
    use_deadline: bool = False
    deadline_pool: str = ""
    deadline_secondary_pool: str = ""
    deadline_group: str = ""
    deadline_priority: int = 50
    deadline_comment: str = ""
    deadline_department: str = ""
    deadline_chunk_size: int = 1
    deadline_suspended: bool = False
    deadline_job_name_template: str = "Render Mapper Pro Job - {scene_name}"
    deadline_machine_limit: int = 0
    deadline_limits: str = ""
    deadline_command_path: str = ""
    deadline_repo_path: str = ""
    deadline_whitelist: str = ""


@dataclass
class JobConfig(DeadlineFields):
    scene_path: str
    video_path: str
    target_material: str
    target_camera: str
    output_path: str
    render: RenderOptions
    safe_mode: bool = True
    submit_scene: bool = True
    force_submit: bool = False   # C4D: submit even if the bake produced no clip frames
    preview_path: str = ""
    preview_frame: int = 0   # >0 → render only this single scene frame (fast preview)
    prepared_blend_path: str = ""   # set → save a mapped .blend here instead of rendering
    pack_blend: bool = False         # pack external files (videos) into the prepared .blend
    ffmpeg_path: str = ""            # bundled ffmpeg, used by the C4D/Redshift worker
    audio_path: str = ""   # legacy single source clip to mux audio from ("" = silent)
    audio_paths: list[str] = field(default_factory=list)  # clips to mux audio from (one strip each)
    material_assignments: list[MaterialVideoAssignment] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        data = asdict(self)
        data["render"]["engine"] = self.render.engine.upper()
        if not data["material_assignments"] and self.target_material and self.video_path:
            data["material_assignments"] = [
                asdict(
                    MaterialVideoAssignment(
                        material_name=self.target_material,
                        video_path=self.video_path,
                    )
                )
            ]
        return data


@dataclass
class RenderJob(DeadlineFields):
    id: int
    video_path: str = ""
    label: str = ""
    custom_label: bool = False   # True once the user renames the job by hand
    output_path: str = ""
    output_input: str = ""
    scene_path: str = ""
    target_camera: str = ""
    output_profile: str = "H264 MP4"
    render_options: RenderOptions | None = None
    safe_mode: bool = True
    status: str = "idle"
    error: str = ""
    attempts: int = 0
    progress: float = 0.0
    selected: bool = True
    # deadline_* + use_deadline come from DeadlineFields; this one is RenderJob-only.
    deadline_submit_scene: bool = True
    material_assignments: list[MaterialVideoAssignment] = field(default_factory=list)
