# app/config.py — 模型与爬虫配置（读写 data/config.json，Key 不入库不进前端代码）
import json
import pathlib

DEFAULT = {
    "api_base": "https://api.example.com/v1",
    "api_key": "",
    "chat_model": "",
    "embedding_model": "",
    "temperature": 0.1,
    # 附加请求头（如网关要求的 x-llm-channel），随请求发送
    "extra_headers": {},
    # 爬虫限频（Fetcher 读取）
    "request_interval_sec": 5,
    "daily_max_per_source": 500,
}


def _config_path(data_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "config.json"


def load_config(data_dir: pathlib.Path) -> dict:
    """读取配置，缺失字段用默认值补齐。

    文件损坏（非法 JSON）时退回默认配置而非抛异常——配置读取被几乎所有
    端点调用，抛 JSONDecodeError 会让整个服务 500。
    """
    cfg = dict(DEFAULT)
    p = _config_path(data_dir)
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in DEFAULT:
                    cfg[k] = v
    return cfg


def save_config(data_dir: pathlib.Path, cfg: dict) -> dict:
    """保存配置（仅保留已知字段），返回归一化后的完整配置。

    临时文件 + os.replace 原子替换：写盘途中崩溃/断电不会留下半个 JSON。
    """
    clean = {k: cfg.get(k, DEFAULT[k]) for k in DEFAULT}
    data_dir = pathlib.Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = _config_path(data_dir).with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(_config_path(data_dir))
    return clean


if __name__ == "__main__":
    # python -m app.config ：快速自检 roundtrip
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        cfg = {"api_base": "http://x/v1", "api_key": "sk-test", "temperature": 0.2}
        merged = save_config(d, cfg)
        loaded = load_config(d)
        assert merged == loaded, "roundtrip mismatch"
        assert loaded["chat_model"] == DEFAULT["chat_model"]  # 缺省回填
        print("config roundtrip OK:", loaded)
