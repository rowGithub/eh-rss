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

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


EH_URL = "https://e-hentai.org/"
EH_API = "https://api.e-hentai.org/api.php"
ATOM_NS = "http://www.w3.org/2005/Atom"

ET.register_namespace(
    "atom",
    ATOM_NS
)

CONFIG_FILE = Path("config.yaml")
STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
    )
}
def create_session():

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,

        backoff_factor=2,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504
        ],

        allowed_methods=[
            "GET",
            "POST"
        ],

        respect_retry_after_header=True
    )

    adapter = HTTPAdapter(
        max_retries=retry
    )

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    session.mount(
        "https://",
        adapter
    )

    session.mount(
        "http://",
        adapter
    )

    return session


SESSION = create_session()

PAGE_REQUEST_DELAY = 3.2

API_BATCH_SIZE = 25
API_REQUESTS_BEFORE_PAUSE = 4
API_PAUSE_SECONDS = 5.2

SEEN_STATE_LIMIT = 20000


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
            "items": [],
            "checkpoint": [],
            "batches": []
        }

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        data.setdefault(
            "seen",
            []
        )

        data.setdefault(
            "items",
            []
        )

        data.setdefault(
            "checkpoint",
            []
        )

        data.setdefault(
            "batches",
            []
        )

        return data

    except Exception:

        return {
            "seen": [],
            "items": [],
            "checkpoint": [],
            "batches": []
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
    previous_checkpoint,
    config
):

    crawl = config.get(
        "crawl",
        {}
    )

    max_pages = int(
        crawl.get(
            "max_pages",
            300
        )
    )

    checkpoint_size = int(
        crawl.get(
            "checkpoint_size",
            20
        )
    )

    checkpoint_tail_size = int(
        crawl.get(
            "checkpoint_tail_size",
            5
        )
    )

    checkpoint_hits_required = int(
        crawl.get(
            "checkpoint_hits_required",
            3
        )
    )

    overlap_pages = int(
        crawl.get(
            "overlap_pages",
            2
        )
    )

    checkpoint_tail_size = max(
        1,
        checkpoint_tail_size
    )

    overlap_pages = max(
        0,
        overlap_pages
    )

    previous_checkpoint = [
        str(x)
        for x in previous_checkpoint
        if str(x)
    ]

    # ========================================================
    # 只使用上一轮 checkpoint 的“尾部”
    #
    # 假设上一轮保存：
    # A B C ... P Q R S T
    #
    # 那么真正的边界锚点只取：
    # P Q R S T
    # ========================================================

    checkpoint_tail = (
        previous_checkpoint[
            -checkpoint_tail_size:
        ]
    )

    checkpoint_tail_set = set(
        checkpoint_tail
    )

    if checkpoint_tail_set:

        checkpoint_hits_required = max(
            1,
            min(
                checkpoint_hits_required,
                len(
                    checkpoint_tail_set
                )
            )
        )

    # ========================================================
    # 特殊模式
    # ========================================================

    first_run = (
        not seen
        and not previous_checkpoint
    )

    migration_mode = (
        bool(seen)
        and not previous_checkpoint
    )

    if first_run:

        max_pages = 1

        print(
            "No previous state. "
            "First-run safety: "
            "latest page only."
        )

    if migration_mode:

        print(
            "Legacy state detected. "
            "One-time checkpoint "
            "migration mode enabled."
        )

    migration_seen_required = max(
        50,
        checkpoint_size * 2
    )

    # ========================================================
    # 本轮状态
    # ========================================================

    current_url = EH_URL

    new_galleries = []

    discovered = set()

    checkpoint_hits = set()

    migration_seen_streak = 0

    # 已经命中 checkpoint 尾部锚点，
    # 但还要继续扫描 overlap_pages
    boundary_confirmed = False

    boundary_page_index = None

    # 真正允许安全停止
    reached_boundary = False

    # 本轮首页前 N 个，
    # 成为下一轮 checkpoint
    next_checkpoint = []

    # ========================================================
    # 自动分页
    # ========================================================

    for page_index in range(
        max_pages
    ):

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

        response = SESSION.get(
            current_url,
            timeout=30
        )

        response.raise_for_status()

        galleries = extract_galleries(
            response.text
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

        # ----------------------------------------------------
        # 保存本轮首页 checkpoint
        # ----------------------------------------------------

        if page_index == 0:

            next_checkpoint = [

                str(gid)

                for gid, token
                in galleries[
                    :checkpoint_size
                ]
            ]

            print(
                "Next checkpoint "
                f"prepared with "
                f"{len(next_checkpoint)} "
                "galleries."
            )

            if checkpoint_tail:

                print(
                    "Previous checkpoint "
                    f"tail contains "
                    f"{len(checkpoint_tail)} "
                    "anchor galleries."
                )

        # ----------------------------------------------------
        # 扫描当前页
        # ----------------------------------------------------

        for gid, token in galleries:

            gid_str = str(gid)

            # ================================================
            # 1. 检查 checkpoint 尾部锚点
            # ================================================

            if (
                checkpoint_tail_set
                and gid_str
                in checkpoint_tail_set
            ):

                # 同一个锚点只计算一次
                if gid_str not in checkpoint_hits:

                    checkpoint_hits.add(
                        gid_str
                    )

                    print(
                        "Checkpoint tail hit: "
                        f"{gid_str} "
                        f"("
                        f"{len(checkpoint_hits)}"
                        f"/"
                        f"{checkpoint_hits_required}"
                        f")"
                    )

                # 第一次达到要求时，
                # 只确认“进入旧边界区”，
                # 此时还不能马上停止。
                if (
                    not boundary_confirmed
                    and len(
                        checkpoint_hits
                    )
                    >= checkpoint_hits_required
                ):

                    boundary_confirmed = True

                    boundary_page_index = (
                        page_index
                    )

                    print(
                        "Checkpoint tail "
                        "boundary confirmed "
                        f"on page "
                        f"{page_index + 1}. "
                        f"Will scan "
                        f"{overlap_pages} "
                        "additional page(s)."
                    )

            # ================================================
            # 2. 收集真正的新 Gallery
            # ================================================

            if (
                gid_str not in seen
                and gid_str
                not in discovered
            ):

                discovered.add(
                    gid_str
                )

                new_galleries.append(
                    (gid, token)
                )

                if migration_mode:

                    migration_seen_streak = 0

            else:

                # 迁移模式下，
                # 只有真正存在于旧 seen 中的项目
                # 才算旧数据连续命中。
                if (
                    migration_mode
                    and gid_str in seen
                ):

                    migration_seen_streak += 1

            # ================================================
            # 3. 一次性的旧 state 迁移逻辑
            # ================================================

            if (
                migration_mode
                and migration_seen_streak
                >= migration_seen_required
            ):

                reached_boundary = True

                print(
                    "Legacy boundary "
                    "reached after "
                    f"{migration_seen_streak} "
                    "consecutive "
                    "previously-seen "
                    "galleries."
                )

                break

        if reached_boundary:

            break

        # ====================================================
        # checkpoint 已确认后，
        # 必须完整多扫描 overlap_pages 页。
        #
        # boundary 在第 1 页确认：
        #
        # overlap_pages = 2
        #
        # 第1页：确认边界
        # 第2页：继续扫
        # 第3页：继续扫
        # 第3页结束后才安全停止
        # ====================================================

        if (
            boundary_confirmed
            and boundary_page_index
            is not None
            and page_index
            >= (
                boundary_page_index
                + overlap_pages
            )
        ):

            reached_boundary = True

            print(
                "Checkpoint boundary "
                "completed after "
                f"{overlap_pages} "
                "overlap page(s)."
            )

            break

        # ----------------------------------------------------
        # 获取真正的 Next >
        # ----------------------------------------------------

        next_url = extract_next_url(
            response.text,
            current_url
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
    # 防漏保险
    #
    # 只要不是第一次初始化：
    #
    # - 没找到 checkpoint 尾部边界
    # 或
    # - 找到了，但 overlap 页数还没有扫完
    #
    # 都直接失败。
    #
    # state / checkpoint 不推进。
    # ========================================================

    if (
        not first_run
        and not reached_boundary
    ):

        raise RuntimeError(

            "Did not safely reach "
            "and pass the previous "
            "crawl boundary within "
            f"{max_pages} pages. "

            "State and checkpoint "
            "were NOT advanced. "

            "Increase crawl.max_pages "
            "and run again."
        )

    if not next_checkpoint:

        next_checkpoint = list(
            previous_checkpoint
        )

    print(
        f"Discovered "
        f"{len(new_galleries)} "
        f"new galleries."
    )

    return (
        new_galleries,
        next_checkpoint
    )

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


        response = SESSION.post(
            EH_API,
            json=payload,
            headers=HEADERS,
            timeout=30
        )


        response.raise_for_status()


        data = response.json()


        returned_metadata = data.get(
            "gmetadata",
            []
        )
        
        expected_gids = {
            str(gid)
            for gid, token in batch
        }
        
        returned_gids = {
            str(item.get("gid"))
            for item in returned_metadata
            if item.get("gid")
        }
        
        missing_gids = (
            expected_gids
            - returned_gids
        )
        
        if missing_gids:
        
            raise RuntimeError(
                "EH API did not return "
                "metadata for GIDs: "
                + ", ".join(
                    sorted(
                        missing_gids,
                        key=int
                    )
                )
            )
        
        all_metadata.extend(
            returned_metadata
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

    male_exact = {
        normalize(x)
        for x in exclude.get(
            "male_exact",
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
            namespace == "male"
            and tag_name in male_exact
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

def create_permanent_batches(
    new_items,
    existing_batches,
    config
):

    feed_config = config.get(
        "feed",
        {}
    )

    batch_size = int(
        feed_config.get(
            "batch_size",
            12
        )
    )

    batch_size = max(
        1,
        batch_size
    )

    if not new_items:

        return existing_batches

    # 保证同一轮里的顺序稳定：
    # 最新 gallery 在前
    new_items = sorted(
        new_items,
        key=lambda x: x.get(
            "posted",
            0
        ),
        reverse=True
    )

    created_at = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    new_batches = []

    batch_number = 0

    for start in range(
        0,
        len(new_items),
        batch_size
    ):

        batch_items = new_items[
            start:
            start + batch_size
        ]

        if not batch_items:

            continue

        batch_number += 1

        first_gid = str(
            batch_items[0]["gid"]
        )

        last_gid = str(
            batch_items[-1]["gid"]
        )

        batch_id = (
            f"eh-batch-"
            f"{created_at}-"
            f"{batch_number}-"
            f"{first_gid}-"
            f"{last_gid}"
        )

        new_batches.append(
            {
                "id": batch_id,
                "created_at": created_at,
                "items": batch_items
            }
        )

        print(
            f"Created permanent batch "
            f"{batch_id}: "
            f"{len(batch_items)} galleries"
        )

    # 新批次放在最前面
    return (
        new_batches
        + existing_batches
    )

def apply_batch_retention(
    batches,
    config
):

    feed_config = config.get(
        "feed",
        {}
    )

    retention_days = int(
        feed_config.get(
            "batch_retention_days",
            3
        )
    )

    max_items = int(
        feed_config.get(
            "batch_max_items",
            200
        )
    )

    retention_days = max(
        1,
        retention_days
    )

    max_items = max(
        1,
        max_items
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

    retained = [
        batch
        for batch in batches
        if int(
            batch.get(
                "created_at",
                0
            )
        ) >= cutoff
    ]

    retained.sort(
        key=lambda x: int(
            x.get(
                "created_at",
                0
            )
        ),
        reverse=True
    )

    retained = retained[
        :max_items
    ]

    print(
        "Batch retention: "
        f"{len(retained)} batches "
        f"(last {retention_days} days, "
        f"max {max_items})."
    )

    return retained

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

def build_feed(
    items,
    output_file=FEED_FILE,
    feed_title="Filtered E-Hentai",
    self_url=None,
    websub_hub=None
):

    rss = ET.Element(
        "rss",
        version="2.0"
    )


    channel = ET.SubElement(
        rss,
        "channel"
    )
    if websub_hub and self_url:

        ET.SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            {
                "href": websub_hub,
                "rel": "hub"
            }
        )
    
        ET.SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            {
                "href": self_url,
                "rel": "self",
                "type": "application/rss+xml"
            }
        )


    ET.SubElement(
        channel,
        "title"
    ).text = feed_title


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
        output_file,
        encoding="utf-8",
        xml_declaration=True
    )


    print(

        f"RSS generated: "
        f"{output_file} "

        f"({len(items)} items)"
    )

def build_sharded_feeds(
    items,
    config
):

    feed_config = config.get(
        "feed",
        {}
    )

    public_base_url = str(
        feed_config.get(
            "public_base_url",
            ""
        )
    ).rstrip("/")
    
    websub_hub = str(
        feed_config.get(
            "websub_hub",
            ""
        )
    ).strip()

    shard_count = int(
        feed_config.get(
            "shard_count",
            1
        )
    )

    shard_count = max(
        1,
        shard_count
    )

    print(
        f"Generating "
        f"{shard_count} "
        f"RSS shard(s)..."
    )

    for shard_index in range(
        shard_count
    ):

        shard_items = [

            item

            for item in items

            if int(
                item["gid"]
            ) % shard_count
            == shard_index
        ]

        # ------------------------------------------------
        # 旧 URL 暂时继续生成，避免现有订阅和工作流中断
        # ------------------------------------------------
        
        old_output_file = Path(
            f"feed-{shard_index}.xml"
        )
        
        old_self_url = (
            f"{public_base_url}/"
            f"feed-{shard_index}.xml"
        )
        
        build_feed(
            shard_items,
            output_file=old_output_file,
            feed_title=(
                "Filtered E-Hentai "
                f"[{shard_index + 1}/"
                f"{shard_count}]"
            ),
            self_url=old_self_url,
            websub_hub=websub_hub
        )
        
        
        # ------------------------------------------------
        # 新的普通 RSS URL
        # Inoreader 从未见过这些地址
        # ------------------------------------------------
        
        new_output_file = Path(
            f"rss-{shard_index}.xml"
        )
        
        new_self_url = (
            f"{public_base_url}/"
            f"rss-{shard_index}.xml"
        )
        
        build_feed(
            shard_items,
            output_file=new_output_file,
            feed_title=(
                "Filtered E-Hentai RSS "
                f"[{shard_index + 1}/"
                f"{shard_count}]"
            ),
            self_url=new_self_url,
            websub_hub=websub_hub
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

    existing_batches = state.get(
        "batches",
        []
    )

    previous_checkpoint = [

        str(x)
    
        for x in state.get(
            "checkpoint",
            []
        )
    ]

    # --------------------------------------------------------
    # 自动翻页直到追上上次位置
    # --------------------------------------------------------

    (
        new_galleries,
        next_checkpoint
    ) = get_new_galleries(
        seen,
        previous_checkpoint,
        config
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

            raise RuntimeError(
                f"EH API error "
                f"for {gid}: "
                f'{meta["error"]}'
            )


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
    # 把本轮新 ACCEPT 的 Gallery 永久封装成 Batch
    # --------------------------------------------------------
    
    all_batches = create_permanent_batches(
        new_items,
        existing_batches,
        config
    )

    all_batches = apply_batch_retention(
        all_batches,
        config
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

    state["checkpoint"] = (
        next_checkpoint
    )

    state["batches"] = (
        all_batches
    )
    
    save_state(
        state
    )


    # 原来的完整 RSS 继续保留
    feed_config = config.get(
        "feed",
        {}
    )
    
    public_base_url = str(
        feed_config.get(
            "public_base_url",
            ""
        )
    ).rstrip("/")
    
    websub_hub = str(
        feed_config.get(
            "websub_hub",
            ""
        )
    ).strip()
    
    build_feed(
        all_items,
        self_url=(
            f"{public_base_url}/feed.xml"
        ),
        websub_hub=websub_hub
    )
    
    # 同时生成分片 RSS
    build_sharded_feeds(
        all_items,
        config
    )
    
    
    print("Done.")


if __name__ == "__main__":

    main()
