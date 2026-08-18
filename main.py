import json
import re
import html
import time
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
import yaml


EH_URL = "https://e-hentai.org/"
EH_API = "https://api.e-hentai.org/api.php"

CONFIG_FILE = Path("config.yaml")
STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
    )
}

PAGE_REQUEST_DELAY = 3.2

API_BATCH_SIZE = 25
API_REQUESTS_BEFORE_PAUSE = 4
API_PAUSE_SECONDS = 5.2

SEEN_STATE_LIMIT = 100000


# ============================================================
# 配置 / 状态
# ============================================================

def load_config():
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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
# 从列表页面取得 gallery
# ============================================================

def extract_galleries(page_html):

    pattern = re.compile(
        r'(?:https?://e-hentai\.org)?/g/(\d+)/([0-9a-f]+)/',
        re.IGNORECASE,
    )

    result = []
    local_seen = set()

    for gid, token in pattern.findall(page_html):

        # 一个 gallery 在网页中可能出现多个链接，
        # 这里只保留一次。
        if gid not in local_seen:

            local_seen.add(gid)

            result.append(
                (int(gid), token)
            )

    return result


# ============================================================
# 找真正的 Next >
# ============================================================

class NextLinkParser(HTMLParser):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.in_anchor = False

        self.href = None

        self.text = []

        self.next_href = None


    def handle_starttag(
        self,
        tag,
        attrs
    ):

        if tag.lower() == "a":

            self.in_anchor = True

            self.href = dict(
                attrs
            ).get("href")

            self.text = []


    def handle_data(
        self,
        data
    ):

        if self.in_anchor:

            self.text.append(
                data
            )


    def handle_endtag(
        self,
        tag
    ):

        if (
            tag.lower() != "a"
            or not self.in_anchor
        ):

            return

        text = re.sub(
            r"\s+",
            " ",
            "".join(self.text)
        ).strip()

        if text == "Next >":

            self.next_href = (
                self.href
            )

        self.in_anchor = False

        self.href = None

        self.text = []


def extract_next_url(
    page_html,
    current_url
):

    parser = NextLinkParser()

    parser.feed(
        page_html
    )

    if not parser.next_href:

        return None

    return urljoin(
        current_url,
        html.unescape(
            parser.next_href
        )
    )


# ============================================================
# 自动往后翻页
# 直到追上上一次已经处理过的位置
# ============================================================

def get_new_galleries(
    seen,
    config
):

    crawl = config.get(
        "crawl",
        {}
    )

    max_pages = int(
        crawl.get(
            "max_pages",
            50
        )
    )

    stop_after_seen = int(
        crawl.get(
            "stop_after_seen",
            5
        )
    )

    # 如果 state.json 丢失，
    # 防止突然把几十页历史全部抓回来。
    if not seen:

        max_pages = 1

        print(
            "No previous state. "
            "First-run safety: "
            "latest page only."
        )


    current_url = EH_URL

    new_galleries = []

    discovered = set()

    consecutive_seen = 0

    reached_boundary = False


    for page_index in range(
        max_pages
    ):

        # EH 搜索请求之间留足间隔
        if page_index > 0:

            time.sleep(
                PAGE_REQUEST_DELAY
            )


        print(
            f"Fetching page "
            f"{page_index + 1}/"
            f"{max_pages}: "
            f"{current_url}"
        )


        response = requests.get(
            current_url,
            headers=HEADERS,
            timeout=30
        )

        response.raise_for_status()


        galleries = (
            extract_galleries(
                response.text
            )
        )


        print(
            f"Found "
            f"{len(galleries)} "
            f"galleries "
            f"on this page."
        )


        if not galleries:

            print(
                "No galleries found. "
                "Stopping."
            )

            break


        for gid, token in galleries:

            gid_str = str(gid)


            # -----------------------------
            # 已经见过
            # -----------------------------

            if gid_str in seen:

                consecutive_seen += 1


                if (
                    consecutive_seen
                    >= stop_after_seen
                ):

                    reached_boundary = True

                    print(
                        "Reached previous "
                        "crawl boundary: "
                        f"{consecutive_seen} "
                        "consecutive "
                        "seen galleries."
                    )

                    break


                continue


            # -----------------------------
            # 当前这次抓取中已经发现过
            # -----------------------------

            if gid_str in discovered:

                continue


            # 发现真正的新 gallery
            consecutive_seen = 0

            discovered.add(
                gid_str
            )

            new_galleries.append(
                (gid, token)
            )


        if reached_boundary:

            break


        next_url = (
            extract_next_url(
                response.text,
                current_url
            )
        )


        if not next_url:

            print(
                "No Next > link. "
                "Stopping."
            )

            break


        if next_url == current_url:

            raise RuntimeError(
                "Next > link did not "
                "advance. "
                "Refusing to loop."
            )


        current_url = next_url


    # ========================================================
    # 极重要：
    #
    # 如果已有历史 state，但翻 max_pages 页仍然没有碰到
    # 上次的位置，则直接失败。
    #
    # 不保存 state，避免把中间未抓到的数据跨过去。
    # ========================================================

    if (
        seen
        and not reached_boundary
    ):

        raise RuntimeError(

            "Did not reach previous "
            "crawl boundary within "
            f"{max_pages} pages. "

            "Increase crawl.max_pages "
            "and run again. "

            "State was NOT advanced."
        )


    print(
        f"Discovered "
        f"{len(new_galleries)} "
        f"new galleries."
    )


    return new_galleries


# ============================================================
# EH 官方 gdata API
# ============================================================

def fetch_metadata(
    galleries
):

    all_metadata = []

    request_number = 0


    for start in range(
        0,
        len(galleries),
        API_BATCH_SIZE
    ):

        # 每 4 次 API 后暂停
        if (
            request_number > 0
            and request_number
            % API_REQUESTS_BEFORE_PAUSE
            == 0
        ):

            print(
                "API safety pause: "
                f"{API_PAUSE_SECONDS}s"
            )

            time.sleep(
                API_PAUSE_SECONDS
            )


        batch = galleries[
            start:
            start + API_BATCH_SIZE
        ]


        payload = {

            "method": "gdata",

            "gidlist": [
                [gid, token]
                for gid, token
                in batch
            ],

            "namespace": 1
        }


        print(
            "Requesting metadata "
            f"for {len(batch)} "
            "galleries..."
        )


        response = requests.post(
            EH_API,
            json=payload,
            headers=HEADERS,
            timeout=30
        )


        response.raise_for_status()


        data = response.json()


        all_metadata.extend(
            data.get(
                "gmetadata",
                []
            )
        )


        request_number += 1


    return all_metadata


# ============================================================
# Tag
# ============================================================

def normalize(text):

    return (
        str(text)
        .strip()
        .casefold()
    )


def split_tag(tag):

    tag = normalize(tag)

    if ":" in tag:

        return tag.split(
            ":",
            1
        )

    return "", tag


# ============================================================
# 黑名单
# ============================================================

def find_reject_reason(
    metadata,
    config
):

    exclude = config.get(
        "exclude",
        {}
    )


    female_exact = {

        normalize(x)

        for x in exclude.get(
            "female_exact",
            []
        )
    }


    any_namespace = {

        normalize(x)

        for x in exclude.get(
            "any_namespace",
            []
        )
    }


    animal_content = {

        normalize(x)

        for x in exclude.get(
            "animal_content",
            []
        )
    }


    for full_tag in metadata.get(
        "tags",
        []
    ):

        namespace, tag_name = (
            split_tag(
                full_tag
            )
        )


        if (
            namespace == "female"
            and tag_name
            in female_exact
        ):

            return full_tag


        if (
            tag_name
            in any_namespace
        ):

            return full_tag


        if (
            tag_name
            in animal_content
        ):

            return full_tag


    return None


# ============================================================
# 文件大小
# ============================================================

def human_size(size):

    try:

        size = int(size)

    except Exception:

        return ""


    mb = (
        size
        / 1024
        / 1024
    )


    if mb < 1024:

        return (
            f"{mb:.1f} MB"
        )


    return (
        f"{mb / 1024:.2f} GB"
    )


# ============================================================
# metadata → RSS item
# ============================================================

def metadata_to_item(meta):

    gid = int(
        meta["gid"]
    )

    token = meta["token"]


    return {

        "gid": gid,

        "token": token,

        "url": (
            "https://e-hentai.org/"
            f"g/{gid}/{token}/"
        ),

        "title":
            meta.get(
                "title",
                ""
            ),

        "title_jpn":
            meta.get(
                "title_jpn",
                ""
            ),

        "category":
            meta.get(
                "category",
                ""
            ),

        "thumb":
            meta.get(
                "thumb",
                ""
            ),

        "uploader":
            meta.get(
                "uploader",
                ""
            ),

        "posted":
            int(
                meta.get(
                    "posted",
                    0
                )
                or 0
            ),

        "filecount":
            str(
                meta.get(
                    "filecount",
                    ""
                )
            ),

        "filesize":
            int(
                meta.get(
                    "filesize",
                    0
                )
                or 0
            ),

        "rating":
            str(
                meta.get(
                    "rating",
                    ""
                )
            ),

        "torrentcount":
            str(
                meta.get(
                    "torrentcount",
                    ""
                )
            ),

        "tags":
            meta.get(
                "tags",
                []
            )
    }


# ============================================================
# RSS 内容
# ============================================================

def build_description(item):

    parts = []


    if item.get("thumb"):

        parts.append(

            '<p><img src="'

            + html.escape(
                item["thumb"]
            )

            + '" style="'
            + 'max-width:300px;">'
            + '</p>'
        )


    if (
        item.get("title_jpn")
        and item["title_jpn"]
        != item.get("title")
    ):

        parts.append(

            "<p>"
            "<b>Japanese title:</b>"
            "<br>"

            + html.escape(
                item["title_jpn"]
            )

            + "</p>"
        )


    parts.append("<p>")


    fields = [

        (
            "Category",
            item.get(
                "category"
            )
        ),

        (
            "Rating",

            (
                f'{item.get("rating")} ★'

                if item.get(
                    "rating"
                )

                else ""
            )
        ),

        (
            "Pages",
            item.get(
                "filecount"
            )
        ),

        (
            "Size",

            (
                human_size(
                    item.get(
                        "filesize"
                    )
                )

                if item.get(
                    "filesize"
                )

                else ""
            )
        ),

        (
            "Uploader",
            item.get(
                "uploader"
            )
        ),

        (
            "Torrents",
            item.get(
                "torrentcount"
            )
        ),
    ]


    for label, value in fields:

        if value:

            parts.append(

                f"<b>{label}:</b> "

                + html.escape(
                    str(value)
                )

                + "<br>"
            )


    parts.append("</p>")


    if item.get("tags"):

        parts.append(
            "<p>"
            "<b>Tags:</b>"
            "<br>"
        )


        for tag in item["tags"]:

            parts.append(

                html.escape(
                    tag
                )

                + "<br>"
            )


        parts.append("</p>")


    parts.append(

        '<p><a href="'

        + html.escape(
            item["url"]
        )

        + '">'
        + 'Open Gallery'
        + '</a></p>'
    )


    return "".join(parts)


# ============================================================
# RSS 保留窗口
# ============================================================

def apply_feed_retention(
    items,
    config
):

    feed_config = config.get(
        "feed",
        {}
    )


    retention_days = int(
        feed_config.get(
            "retention_days",
            7
        )
    )


    max_items = int(
        feed_config.get(
            "max_items",
            5000
        )
    )


    cutoff = int(

        (
            datetime.now(
                timezone.utc
            )

            - timedelta(
                days=retention_days
            )

        ).timestamp()
    )


    retained = []


    for item in items:

        posted = int(
            item.get(
                "posted",
                0
            )
            or 0
        )


        if (
            posted == 0
            or posted >= cutoff
        ):

            retained.append(
                item
            )


    retained.sort(

        key=lambda x:
            x.get(
                "posted",
                0
            ),

        reverse=True
    )


    retained = retained[
        :max_items
    ]


    print(

        "Feed retention: "

        f"{len(retained)} items "

        f"(last "
        f"{retention_days} days, "

        f"max {max_items})."
    )


    return retained


# ============================================================
# 生成 RSS 2.0
# ============================================================

def build_feed(items):

    rss = ET.Element(
        "rss",
        version="2.0"
    )


    channel = ET.SubElement(
        rss,
        "channel"
    )


    ET.SubElement(
        channel,
        "title"
    ).text = (
        "Filtered E-Hentai"
    )


    ET.SubElement(
        channel,
        "link"
    ).text = EH_URL


    ET.SubElement(
        channel,
        "description"
    ).text = (

        "Filtered E-Hentai "
        "galleries generated "
        "by GitHub Actions"
    )


    ET.SubElement(
        channel,
        "language"
    ).text = "en"


    ET.SubElement(
        channel,
        "lastBuildDate"
    ).text = format_datetime(

        datetime.now(
            timezone.utc
        )
    )


    for item in items:

        entry = ET.SubElement(
            channel,
            "item"
        )


        title_parts = []


        if item.get(
            "rating"
        ):

            title_parts.append(
                f'[{item["rating"]}★]'
            )


        if item.get(
            "category"
        ):

            title_parts.append(
                f'[{item["category"]}]'
            )


        title_parts.append(
            item.get(
                "title",
                ""
            )
        )


        ET.SubElement(
            entry,
            "title"
        ).text = " ".join(
            title_parts
        )


        ET.SubElement(
            entry,
            "link"
        ).text = item["url"]


        guid = ET.SubElement(
            entry,
            "guid",
            isPermaLink="false"
        )


        guid.text = (
            f'eh-{item["gid"]}'
        )


        if item.get(
            "posted"
        ):

            dt = (
                datetime
                .fromtimestamp(
                    item["posted"],
                    tz=timezone.utc
                )
            )


            ET.SubElement(
                entry,
                "pubDate"
            ).text = (
                format_datetime(dt)
            )


        ET.SubElement(
            entry,
            "description"
        ).text = (
            build_description(
                item
            )
        )


        for tag in item.get(
            "tags",
            []
        ):

            ET.SubElement(
                entry,
                "category"
            ).text = tag


    tree = ET.ElementTree(
        rss
    )


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
# MAIN
# ============================================================

def main():

    config = load_config()

    state = load_state()


    seen = {

        str(x)

        for x in state.get(
            "seen",
            []
        )
    }


    old_items = state.get(
        "items",
        []
    )


    # --------------------------------------------------------
    # 自动翻页直到追上上次位置
    # --------------------------------------------------------

    new_galleries = (
        get_new_galleries(
            seen,
            config
        )
    )


    # --------------------------------------------------------
    # 批量取得 metadata
    # --------------------------------------------------------

    metadata_list = (

        fetch_metadata(
            new_galleries
        )

        if new_galleries

        else []
    )


    new_items = []


    # --------------------------------------------------------
    # 黑名单过滤
    # --------------------------------------------------------

    for meta in metadata_list:

        gid = str(
            meta.get(
                "gid",
                ""
            )
        )


        if not gid:

            continue


        # API 单项错误不加入 seen
        # 下一次继续尝试
        if "error" in meta:

            print(
                f"API error "
                f"for {gid}: "
                f'{meta["error"]}'
            )

            continue


        if meta.get(
            "expunged"
        ):

            print(
                f"SKIP {gid}: "
                "expunged"
            )

            seen.add(gid)

            continue


        reason = (
            find_reject_reason(
                meta,
                config
            )
        )


        seen.add(gid)


        if reason:

            print(
                f"REJECT "
                f"{gid}: "
                f"{reason}"
            )

            continue


        print(
            f"ACCEPT "
            f"{gid}: "
            f'{meta.get("title", "")}'
        )


        new_items.append(
            metadata_to_item(
                meta
            )
        )


    # --------------------------------------------------------
    # 新旧 RSS 项目合并并按 gid 去重
    # --------------------------------------------------------

    unique = {}


    for item in (
        new_items
        + old_items
    ):

        gid = str(
            item.get(
                "gid"
            )
        )


        if gid not in unique:

            unique[gid] = item


    all_items = (
        list(
            unique.values()
        )
    )


    # --------------------------------------------------------
    # RSS 保留最近 7 天
    # --------------------------------------------------------

    all_items = (
        apply_feed_retention(
            all_items,
            config
        )
    )


    # --------------------------------------------------------
    # seen 防止无限膨胀
    # --------------------------------------------------------

    sorted_seen = sorted(

        seen,

        key=lambda x:
            int(x),

        reverse=True
    )


    state["seen"] = (
        sorted_seen[
            :SEEN_STATE_LIMIT
        ]
    )


    state["items"] = (
        all_items
    )


    save_state(
        state
    )


    build_feed(
        all_items
    )


    print("Done.")


if __name__ == "__main__":

    main()
