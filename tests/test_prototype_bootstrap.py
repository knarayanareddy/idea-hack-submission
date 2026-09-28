from pathlib import Path
import tomllib


def test_pytest_configuration_is_discoverable() -> None:
    project_root = Path(__file__).resolve().parents[1]
    config_path = project_root / "pyproject.toml"

    assert config_path.is_file()
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "pytest" in config.get("tool", {})
