#!/usr/bin/env python3
"""一键部署到百炼 Managed Agent（只用 Python 标准库）。

步骤：打包两个 Skill → 上传并等审核 active → 建运行环境（预装 flyai-cli@1.0.16、网络 unrestricted）并等就绪
      → 建智能体（系统提示词 + 内置工具 + 两个 Skill）→ 可选：建测试会话并冒烟测试

凭证（都不会打印、不会写进仓库）：
- ~/.flex-date-saver.env：DASHSCOPE_API_KEY=... 和 WS=ws-xxxx
- 飞猪 Key：环境变量 FLYAI_API_KEY，或 ~/.flyai/config.json

用法：
  python3 tools/deploy_bailian.py --form-url https://<user>.github.io/flex-date-saver/web/ [--model qwen3.8-max] [--smoke]
  python3 tools/deploy_bailian.py --only skills      # 只重新上传 Skill（新版本）
状态记录在 tools/.deploy_state.json（已 gitignore），重复执行会复用已建资源。
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "tools" / ".deploy_state.json"
DIST = ROOT / "dist"
FLYAI_SKILL_ZIP_URL = "https://github.com/alibaba-flyai/flyai-skill/archive/refs/heads/main.zip"


def load_env():
    env = {}
    p = Path.home() / ".flex-date-saver.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.setdefault("DASHSCOPE_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
    env.setdefault("WS", os.environ.get("WS", ""))
    if not env["DASHSCOPE_API_KEY"] or not env["WS"]:
        sys.exit("缺少百炼凭证：请在 ~/.flex-date-saver.env 写入 DASHSCOPE_API_KEY 和 WS")
    fk = os.environ.get("FLYAI_API_KEY")
    if not fk:
        c = Path.home() / ".flyai" / "config.json"
        if c.exists():
            fk = json.loads(c.read_text()).get("FLYAI_API_KEY")
    env["FLYAI_API_KEY"] = fk or ""
    return env


class Api:
    def __init__(self, env):
        self.base = f"https://{env['WS']}.cn-beijing.maas.aliyuncs.com/api/v1/agentstudio"
        self.key = env["DASHSCOPE_API_KEY"]

    def req(self, method, path, body=None, headers=None, raw=None):
        h = {"Authorization": f"Bearer {self.key}"}
        data = None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                txt = resp.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:600]
            raise SystemExit(f"[{method} {path}] HTTP {e.code}: {msg}")

    def upload(self, path):
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                   f"Content-Type: application/zip\r\n\r\n".encode())
        body.write(path.read_bytes())
        body.write(f"\r\n--{boundary}--\r\n".encode())
        return self.req("POST", "/files", raw=body.getvalue(),
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def zip_dir(src, out):
    """根目录直接是 SKILL.md（不能再套一层目录）；排除缓存与测试产物。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc" or "cache" in f.parts:
                continue
            z.write(f, f.relative_to(src).as_posix())
    names = zipfile.ZipFile(out).namelist()
    assert "SKILL.md" in names, f"{out} 根目录缺 SKILL.md"
    return out, names


def build_flyai_skill_zip():
    out = DIST / "flyai.zip"
    vendor = ROOT / "tools" / "vendor" / "flyai"
    if not (vendor / "SKILL.md").exists():
        print("下载飞猪官方 Skill …")
        data = urllib.request.urlopen(FLYAI_SKILL_ZIP_URL, timeout=120).read()
        z = zipfile.ZipFile(io.BytesIO(data))
        prefix = next(n for n in z.namelist() if n.endswith("skills/flyai/SKILL.md")).rsplit("SKILL.md", 1)[0]
        for n in z.namelist():
            if n.startswith(prefix) and not n.endswith("/"):
                t = vendor / n[len(prefix):]
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(z.read(n))
    # 运行环境已固定 1.0.16，Skill 说明里的安装命令也固定版本，避免智能体自行装成其它版本
    sk = (vendor / "SKILL.md").read_text()
    sk = sk.replace("npm i -g @fly-ai/flyai-cli`", "npm i -g @fly-ai/flyai-cli@1.0.16`")
    (vendor / "SKILL.md").write_text(sk)
    return zip_dir(vendor, out)


def ensure_skill(api, st, key, zpath, force_new_version=False):
    f = api.upload(zpath)
    fid = f["id"]
    print(f"  上传 {zpath.name} → {fid}（{f.get('status')}）")
    if key in st and force_new_version:
        r = api.req("POST", f"/skills/{st[key]['id']}/versions", {"file_id": fid})
        sid = st[key]["id"]
    elif key in st:
        return st[key]
    else:
        r = api.req("POST", "/skills", {"file_id": fid})
        sid = r["id"]
    for _ in range(60):  # 官方实测审核约 4 分钟
        info = api.req("GET", f"/skills/{sid}")
        if info.get("status") == "active":
            st[key] = {"id": sid, "version": info["latest_version"], "name": info.get("name")}
            save(st)
            print(f"  Skill {info.get('name')} 审核通过：{sid}@{info['latest_version']}")
            return st[key]
        if info.get("status") == "rejected":
            raise SystemExit(f"Skill 审核未通过：{json.dumps(info, ensure_ascii=False)[:600]}")
        time.sleep(10)
    raise SystemExit("Skill 审核超时（10 分钟），稍后用同样命令重试")


def ensure_env(api, st):
    if "environment" in st:
        return st["environment"]
    r = api.req("POST", "/environments", {
        "name": "flex-date-saver-env",
        "description": "弹性日期省钱规划：预装 flyai-cli 1.0.16，网络不限制",
        "config": {"type": "cloud", "packages": {"npm": ["@fly-ai/flyai-cli@1.0.16"]},
                   "networking": {"type": "unrestricted"}},
    })
    st["environment"] = {"id": r["id"]}
    save(st)
    print(f"  运行环境已创建 {r['id']}，等待预热 …")
    return st["environment"]


def ensure_agent(api, st, model, form_url, skills):
    system = (ROOT / "prompts" / "system_prompt.md").read_text().replace("{{FORM_URL}}", form_url)
    body = {
        "name": "flex-date-saver", "model": {"id": model}, "system": system,
        "tools": [{"type": "builtin_toolkit", "default_config": {"enabled": True},
                   "configs": [{"name": n, "enabled": True} for n in ("bash", "read", "write", "edit", "glob", "grep")]}],
        "skills": [{"type": "customer", "skill_id": s["id"], "version": s["version"]} for s in skills],
    }
    if "agent" in st:
        r = api.req("POST", f"/agents/{st['agent']['id']}", body)  # 更新
    else:
        r = api.req("POST", "/agents", body)
    st["agent"] = {"id": r["id"], "version": r.get("version")}
    save(st)
    print(f"  智能体 {r['id']} 版本 {r.get('version')}")
    return st["agent"]


def smoke(api, st, env):
    if not env["FLYAI_API_KEY"]:
        print("  未找到飞猪 Key，跳过冒烟测试")
        return
    for i in range(30):
        try:
            s = api.req("POST", "/sessions", {"agent": st["agent"]["id"], "environment_id": st["environment"]["id"],
                                             "title": "flex-date-saver 冒烟测试",
                                             "environment_variables": {"FLYAI_API_KEY": env["FLYAI_API_KEY"]}})
            break
        except SystemExit as e:
            if "still being prepared" in str(e) and i < 29:
                time.sleep(20)
                continue
            raise
    sid = s["id"]
    st["smoke_session"] = sid
    save(st)
    print(f"  测试会话 {sid}")
    prompt = ("请执行 `which flyai && flyai --help | head -3 && flyai search-flight --origin 上海 --destination 三亚 "
              "--dep-date 2026-11-20 --sort-type 3 | head -c 600`，原样贴出输出，并告诉我输出里有没有「体验模式」四个字。")
    api.req("POST", f"/sessions/{sid}/events",
            {"input": [{"role": "user", "type": "message", "content": [{"type": "text", "text": prompt}]}]})
    print("  已发送冒烟消息，请在控制台该会话里查看结果（应有 search-train 命令、无「体验模式」）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--form-url", default="（表单链接待发布）")
    ap.add_argument("--model", default="qwen3.8-max")
    ap.add_argument("--only", choices=["skills", "env", "agent"])
    ap.add_argument("--new-version", action="store_true", help="已有 Skill 时上传新版本并更新智能体挂载")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    env = load_env()
    api, st = Api(env), state()

    print("1/4 打包 Skill")
    own, names = zip_dir(ROOT / "skill" / "flex-date-saver", DIST / "flex-date-saver.zip")
    print(f"  flex-date-saver.zip：{len(names)} 个文件，{own.stat().st_size // 1024} KB")
    fz, fnames = build_flyai_skill_zip()
    print(f"  flyai.zip：{len(fnames)} 个文件")
    if a.only in (None, "skills"):
        print("2/4 上传 Skill 并等待审核")
        s1 = ensure_skill(api, st, "skill_own", own, a.new_version)
        s2 = ensure_skill(api, st, "skill_flyai", fz, a.new_version)
    else:
        s1, s2 = st["skill_own"], st["skill_flyai"]
    if a.only in (None, "env"):
        print("3/4 运行环境")
        ensure_env(api, st)
    if a.only in (None, "agent", "skills"):
        print("4/4 智能体")
        ensure_agent(api, st, a.model, a.form_url, [s1, s2])
    if a.smoke:
        smoke(api, st, env)
    print("完成。资源 ID 记录在 tools/.deploy_state.json")


if __name__ == "__main__":
    main()
