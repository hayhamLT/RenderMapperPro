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
    video_quality: str = "HIGH"          # FFMPEG constant_rate_factor
    video_codec: str = ""                # optional codec override (e.g. H265); blank = profile default


@dataclass
class MaterialVideoAssignment:
    material_name: str
    video_path: str
    mapping_mode: str = VIDEO_MAPPING_MODE_EMISSION


@dataclass
class JobConfig:
    scene_path: str
    video_path: str
    target_material: str
    target_camera: str
    output_path: str
    render: RenderOptions
    safe_mode: bool = True
    use_deadline: bool = False
    deadline_pool: str = ""
    deadline_secondary_pool: str = ""
    deadline_group: str = ""
    deadline_priority: int = 50
    deadline_comment: str = ""
    deadline_department: str = ""
    deadline_chunk_size: int = 1
    deadline_suspended: bool = False
    deadline_job_name_template: str = "BlenderRender Job - {scene_name}"
    deadline_machine_limit: int = 0
    deadline_limits: str = ""
    deadline_command_path: str = ""
    deadline_repo_path: str = ""
    deadline_whitelist: str = ""
    submit_scene: bool = True
    preview_path: str = ""
    preview_frame: int = 0   # >0 → render only this single scene frame (fast preview)
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
