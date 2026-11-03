"""A local video supplies still previews and simulated shutter frames."""

from dataclasses import dataclass
import math
from pathlib import Path
import time

import av

from camera import CameraFrame


class VideoEnded(Exception):
    """Normal completion of a demo video, not a camera failure."""


def open_video(path):
    # Accept self-contained movie files, not playlists or network sources.
    return av.open(str(path), options={"format_whitelist": "mov,matroska,webm,avi",
                                       "protocol_whitelist": "file"})


def frame_image(frame):
    image = frame.to_image()
    return image.rotate(frame.rotation, expand=True) if frame.rotation else image


@dataclass(frozen=True)
class DemoVideo:
    path: Path
    name: str
    duration: float
    width: int
    height: int

    @classmethod
    def inspect(cls, path, name):
        try:
            with open_video(path) as container:
                if not container.streams.video:
                    raise ValueError("Choose a file with a video track.")
                stream = container.streams.video[0]
                duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                            else float(container.duration or 0) / av.time_base)
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("The video must have a known, positive duration.")
                image = frame_image(next(container.decode(stream)))
                return cls(Path(path), name, duration, image.width, image.height)
        except (av.FFmpegError, StopIteration) as exc:
            raise ValueError("Could not read this video. Try an MP4, MOV, WebM, MKV, or AVI file.") from exc

    def metadata(self):
        return {"mode": "video", "id": self.path.parent.name, "name": self.name,
                "duration_s": self.duration, "width": self.width, "height": self.height}


class VideoCamera:
    """Advance explicitly between decisions; captures preserve the evaluated frame."""

    capabilities = {"live_view": True, "capture_ready": True, "preview_issues": [], "capture_issues": []}

    def __init__(self, video, output, *, enable_capture=False):
        self.video, self.output = video, Path(output)
        self.enable_capture = enable_capture
        self.model = f"Demo video · {video.name}"
        self.container = None
        self.frame_no = 0
        self.position = 0
        self.current_image = None
        self.last_frame_position = None

    def __enter__(self):
        self.container = open_video(self.video.path)
        self.stream = self.container.streams.video[0]
        return self

    @property
    def remaining_seconds(self):
        return max(0, self.video.duration - self.position)

    def _frame_at(self, seconds):
        if seconds >= self.video.duration:
            raise VideoEnded()
        origin = self.stream.start_time or 0
        offset = origin + int(seconds / self.stream.time_base)
        try:
            self.container.seek(offset, stream=self.stream, backward=True)
            chosen, position = None, 0
            for frame in self.container.decode(self.stream):
                current = float(frame.pts * frame.time_base - origin * self.stream.time_base)
                if current > seconds and chosen is not None:
                    break
                chosen, position = frame, max(0, current)
                if current >= seconds:
                    break
            if chosen is None:
                raise RuntimeError("The video has no decodable frame at this position.")
            return frame_image(chosen), position
        except av.FFmpegError as exc:
            raise RuntimeError("Could not decode the video frame.") from exc

    def latest_frame(self):
        requested = time.monotonic()
        image, self.last_frame_position = self._frame_at(self.position)
        self.current_image = image
        self.frame_no += 1
        return CameraFrame(image, requested, self.frame_no)

    def capture(self, capture_id):
        if not self.enable_capture:
            raise RuntimeError("Shutter control is disabled in preview mode")
        if type(capture_id) is not int or capture_id < 0:
            raise ValueError("Invalid demo capture request.")
        image = self.current_image
        if image is None:
            raise RuntimeError("Read a preview before capturing.")
        folder = self.output / f"{capture_id:05d}"
        self.output.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        path = folder / "frame.jpg"
        image.save(path, quality=95, subsampling=0)
        return [path]

    def __exit__(self, *_):
        if self.container:
            self.container.close()
            self.container = None
