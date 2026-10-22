import json
from pathlib import Path

from ueperf.analyze import analyze
from ueperf.config import load_thresholds
from ueperf.ingest.frames import load_frames
from ueperf.report import write_report

FIXTURES = Path(__file__).parent / "fixtures"


def test_report_without_trace(tmp_path):
    src = FIXTURES / "spike2"
    (tmp_path / "frames.csv").write_bytes((src / "frames.csv").read_bytes())
    (tmp_path / "run_info.txt").write_bytes((src / "run_info.txt").read_bytes())
    frames = load_frames(tmp_path / "frames.csv")
    result = analyze(frames, load_thresholds())
    (tmp_path / "analysis.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")

    html = write_report(tmp_path).read_text(encoding="utf-8")
    assert "<svg" in html and "budget 11.1 ms" in html
    assert "No trace was analyzed for this run." in html
    assert html.count('data-chart="') == 2
    for n in range(1, len(result.hitches) + 1):
        assert f"H{n}" in html
    assert "build_config=Development" in html
