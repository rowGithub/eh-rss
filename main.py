import json
import re
import html
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime
import xml.etree.ElementTree as ET

import requests
import yaml


# ============================================================
# 基本设置
# ============================================================

EH_URL = "https://e-hentai.org/"
EH_API = "https://api.e-hentai.org/api.php"

CONFIG_FILE = Path("config.yaml")
STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

DEFAULT_MAX_FEED_ITEMS = 200

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
    )
}


# ============================================================
# 读取配置
# ============================================================

def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ============================================================
# 状态保存
#
# seen:
#   已经检查过的 gallery，避免每次重新处理
#
# items:
#   已经通过过滤、需要保留在 RSS 中的 gallery
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {
            "seen": [],
            "items": []
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("seen", [])
        data.setdefault("items", [])

        return data

    except Exception:
        return {
            "seen": [],
            "items": []
        }


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 从 E-Hentai 首页取得最新 Gallery 的 gid + token
# ============================================================

def get_latest_galleries():
    print("Fetching latest E-Hentai galleries...")

    response = requests.get(
        EH_URL,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    # Gallery URL 格式：
    # https://e-hentai.org/g/1234567/abcdef1234/
    pattern = re.compile(
        r'(?:https?://e-hentai\.org)?/g/(\d+)/([0-9a-f]+)/',
        re.IGNORECASE
    )

    matches = pattern.findall(response.text)

    # 页面中同一个 Gallery 链接可能出现多次
    # 用 gid 去重，同时保持网页原有顺序
    result = []
    seen_gid = set()

    for gid, token in matches:
        if gid not in seen_gid:
            seen_gid.add(gid)
            result.append((int(gid), token))

    print(f"Found {len(result)} galleries on latest page.")

    return result


# ============================================================
# 调用官方 gdata API
# ============================================================

def fetch_metadata(galleries):
    all_metadata = []

    # 官方 API 每次最多 25 个
    for start in range(0, len(galleries), 25):
        batch = galleries[start:start + 25]

        payload = {
            "method": "gdata",
            "gidlist": [
                [gid, token]
                for gid, token in batch
            ],
            "namespace": 1
        }

        print(f"Requesting metadata for {len(batch)} galleries...")

        response = requests.post(
            EH_API,
            json=payload,
            headers=HEADERS,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        all_metadata.extend(
            data.get("gmetadata", [])
        )

    return all_metadata


# ============================================================
# Tag 处理
# ============================================================

def normalize(text):
    return str(text).strip().casefold()


def split_tag(tag):
    """
    female:big ass
        ->
    namespace = female
    name      = big ass

    如果没有 namespace：
    bestiality
        ->
    namespace = ""
    name      = bestiality
    """

    tag = normalize(tag)

    if ":" in tag:
        namespace, name = tag.split(":", 1)
        return namespace, name

    return "", tag


# ============================================================
# 核心过滤逻辑
# ============================================================

def find_reject_reason(metadata, config):
    exclude = config.get("exclude", {})

    female_exact = {
        normalize(x)
        for x in exclude.get("female_exact", [])
    }

    any_namespace = {
        normalize(x)
        for x in exclude.get("any_namespace", [])
    }

    animal_content = {
        normalize(x)
        for x in exclude.get("animal_content", [])
    }

    tags = metadata.get("tags", [])

    for full_tag in tags:

        namespace, tag_name = split_tag(full_tag)

        # ----------------------------------------
        # 1. 只过滤 female namespace
        # ----------------------------------------

        if (
            namespace == "female"
            and tag_name in female_exact
        ):
            return full_tag

        # ----------------------------------------
        # 2. 不管 namespace，只要 tag 名称命中
        # ----------------------------------------

        if tag_name in any_namespace:
            return full_tag

        # ----------------------------------------
        # 3. 动物相关黑名单
        # ----------------------------------------

        if tag_name in animal_content:
            return full_tag

    return None


# ============================================================
# 文件大小显示
# ============================================================

def human_size(size):
    try:
        size = int(size)
    except Exception:
        return ""

    mb = size / 1024 / 1024

    if mb < 1024:
        return f"{mb:.1f} MB"

    return f"{mb / 1024:.2f} GB"


# ============================================================
# 把 API metadata 转成 RSS 保存的数据
# ============================================================

def metadata_to_item(meta):
    gid = int(meta["gid"])
    token = meta["token"]

    return {
        "gid": gid,
        "token": token,

        "url": (
            f"https://e-hentai.org/g/"
            f"{gid}/{token}/"
        ),

        "title": meta.get("title", ""),
        "title_jpn": meta.get("title_jpn", ""),

        "category": meta.get("category", ""),

        "thumb": meta.get("thumb", ""),

        "uploader": meta.get("uploader", ""),

        "posted": int(meta.get("posted", 0) or 0),

        "filecount": str(
            meta.get("filecount", "")
        ),

        "filesize": int(
            meta.get("filesize", 0) or 0
        ),

        "rating": str(
            meta.get("rating", "")
        ),

        "torrentcount": str(
            meta.get("torrentcount", "")
        ),

        "tags": meta.get("tags", [])
    }


# ============================================================
# RSS 正文
# ============================================================

def build_description(item):
    parts = []

    thumb = item.get("thumb", "")

    if thumb:
        parts.append(
            f'<p>'
            f'<img src="{html.escape(thumb)}" '
            f'style="max-width:300px;">'
            f'</p>'
        )

    title_jpn = item.get("title_jpn", "")

    if (
        title_jpn
        and title_jpn != item.get("title", "")
    ):
        parts.append(
            "<p><b>Japanese title:</b><br>"
            + html.escape(title_jpn)
            + "</p>"
        )

    parts.append("<p>")

    if item.get("category"):
        parts.append(
            "<b>Category:</b> "
            + html.escape(item["category"])
            + "<br>"
        )

    if item.get("rating"):
        parts.append(
            "<b>Rating:</b> "
            + html.escape(item["rating"])
            + " ★<br>"
        )

    if item.get("filecount"):
        parts.append(
            "<b>Pages:</b> "
            + html.escape(item["filecount"])
            + "<br>"
        )

    if item.get("filesize"):
        parts.append(
            "<b>Size:</b> "
            + human_size(item["filesize"])
            + "<br>"
        )

    if item.get("uploader"):
        parts.append(
            "<b>Uploader:</b> "
            + html.escape(item["uploader"])
            + "<br>"
        )

    if item.get("torrentcount"):
        parts.append(
            "<b>Torrents:</b> "
            + html.escape(item["torrentcount"])
            + "<br>"
        )

    parts.append("</p>")

    tags = item.get("tags", [])

    if tags:
        parts.append("<p><b>Tags:</b><br>")

        for tag in tags:
            parts.append(
                html.escape(tag) + "<br>"
            )

        parts.append("</p>")

    parts.append(
        '<p><a href="'
        + html.escape(item["url"])
        + '">Open Gallery</a></p>'
    )

    return "".join(parts)


# ============================================================
# 生成标准 RSS 2.0
# ============================================================

def build_feed(items):
    rss = ET.Element(
        "rss",
        version="2.0"
    )

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(
        channel,
        "title"
    ).text = "Filtered E-Hentai"

    ET.SubElement(
        channel,
        "link"
    ).text = EH_URL

    ET.SubElement(
        channel,
        "description"
    ).text = (
        "Filtered E-Hentai galleries "
        "generated by GitHub Actions"
    )

    ET.SubElement(
        channel,
        "language"
    ).text = "en"

    ET.SubElement(
        channel,
        "lastBuildDate"
    ).text = format_datetime(
        datetime.now(timezone.utc)
    )

    for item in items:

        entry = ET.SubElement(
            channel,
            "item"
        )

        rating = item.get("rating", "")
        category = item.get("category", "")
        title = item.get("title", "")

        title_parts = []

        if rating:
            title_parts.append(
                f"[{rating}★]"
            )

        if category:
            title_parts.append(
                f"[{category}]"
            )

        title_parts.append(title)

        ET.SubElement(
            entry,
            "title"
        ).text = " ".join(title_parts)

        ET.SubElement(
            entry,
            "link"
        ).text = item["url"]

        guid = ET.SubElement(
            entry,
            "guid",
            isPermaLink="false"
        )

        guid.text = f"eh-{item['gid']}"

        posted = item.get("posted", 0)

        if posted:
            dt = datetime.fromtimestamp(
                posted,
                tz=timezone.utc
            )

            ET.SubElement(
                entry,
                "pubDate"
            ).text = format_datetime(dt)

        ET.SubElement(
            entry,
            "description"
        ).text = build_description(item)

        # 把 EH tag 同时写成 RSS category
        for tag in item.get("tags", []):
            ET.SubElement(
                entry,
                "category"
            ).text = tag

    tree = ET.ElementTree(rss)

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        FEED_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    print(
        f"RSS generated: "
        f"{FEED_FILE} "
        f"({len(items)} items)"
    )


# ============================================================
# 主程序
# ============================================================

def main():

    config = load_config()
    state = load_state()

    seen = {
        str(x)
        for x in state.get("seen", [])
    }

    old_items = state.get("items", [])

    # ----------------------------------------
    # 获取 EH 当前最新 Gallery
    # ----------------------------------------

    galleries = get_latest_galleries()

    # 只请求之前没有处理过的项目
    new_galleries = [
        (gid, token)
        for gid, token in galleries
        if str(gid) not in seen
    ]

    print(
        f"{len(new_galleries)} new galleries "
        f"need checking."
    )

    # ----------------------------------------
    # API metadata
    # ----------------------------------------

    metadata_list = []

    if new_galleries:
        metadata_list = fetch_metadata(
            new_galleries
        )

    new_items = []

    # ----------------------------------------
    # 逐个过滤
    # ----------------------------------------

    for meta in metadata_list:

        gid = str(meta.get("gid", ""))

        if not gid:
            continue

        # API 错误时暂时不加入 seen，
        # 下次还能重新尝试
        if "error" in meta:
            print(
                f"API error for {gid}: "
                f"{meta['error']}"
            )
            continue

        # 已删除 / expunged Gallery 不进入 RSS
        if meta.get("expunged"):
            print(
                f"SKIP {gid}: expunged"
            )

            seen.add(gid)
            continue

        reason = find_reject_reason(
            meta,
            config
        )

        seen.add(gid)

        if reason:
            print(
                f"REJECT {gid}: {reason}"
            )
            continue

        print(
            f"ACCEPT {gid}: "
            f"{meta.get('title', '')}"
        )

        new_items.append(
            metadata_to_item(meta)
        )

    # ----------------------------------------
    # 合并新旧 RSS 项目
    # ----------------------------------------

    all_items = (
        new_items
        + old_items
    )

    # gid 去重
    unique = {}

    for item in all_items:
        gid = str(item.get("gid"))

        if gid not in unique:
            unique[gid] = item

    all_items = list(
        unique.values()
    )

    # 最新的放最前面
    all_items.sort(
        key=lambda x: x.get("posted", 0),
        reverse=True
    )

    # RSS 最多保留多少条
    max_items = (
        config
        .get("feed", {})
        .get(
            "max_items",
            DEFAULT_MAX_FEED_ITEMS
        )
    )

    all_items = all_items[:max_items]

    # ----------------------------------------
    # 保存
    # ----------------------------------------

    state["seen"] = sorted(
        seen,
        key=lambda x: int(x)
    )

    state["items"] = all_items

    save_state(state)
    build_feed(all_items)

    print("Done.")


if __name__ == "__main__":
    main()
