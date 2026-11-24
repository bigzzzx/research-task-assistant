"""Recognize account-level output preferences in the current user's words."""

import re
import unicodedata

RESULT_ORDER = "result_order"
GC_DECIMALS = "gc_decimal_places"

_SCOPE = re.compile(
    r"(?P<example>例如|比如|举(?:个|一个)?例|假设|假如|如果|示例|(?:有人|用户|别人)(?:说|问))"
    r"|(?P<temporary>这次|此次|本次|本轮|这轮|这一轮|这回|这批|下次|下一次|下一轮)"
    r"|(?P<durable>从今以后|从现在开始|从下次开始|以后|今后|往后|未来都|每次|长期偏好|默认|记住)"
)
_NO_SAVE = re.compile(r"(?:别|不要|不用|无需|不必)[^。；，,]{0,12}(?:记住|保存|记录|设为默认|作为默认)")
_READ_BACK = re.compile(
    r"^(?:请问\s*)?(?:我之前|我以前|我上次|查看|查询|你记得|你还记得|你能否查|如何|为什么|(?:请|能否|能不能)?举)"
)
_DEFAULT_SUFFIX = re.compile(r"(?:设为|设成|作为|改为)(?:我的)?默认(?:设置|格式|偏好)?\s*$")
_PRECISE = re.compile(r"(?<![0-9零〇一二两三四五六七八九十百])([012零〇一二两])\s*位(?:\s*小数|(?=\s*(?:[,，]|$)))")
_NUMBERS = {"0": 0, "零": 0, "〇": 0, "1": 1, "一": 1, "2": 2, "二": 2, "两": 2}
_FILE_ORDER = re.compile(
    r"(?:按照?|保持|保留|沿用|使用|用)\s*(?:fasta\s*)?"
    r"(?:原始(?:文件|记录)?|原(?:文件|记录)?|文件(?:中的?)?(?:记录)?|输入(?:文件|记录)?|上传(?:文件)?|fasta)"
    r"(?:的)?\s*(?:顺序|次序)", re.I,
)
_ID_ORDER = re.compile(r"(?:按照?|依照|使用|用)\s*(?:序列\s*)?id(?![a-z0-9])", re.I)
_NEGATED_FORMAT = re.compile(r"(?:不要|不用|别|无需)\s*(?:再\s*)?(?:保留|显示|展示|用|按)?\s*$")
_GC = re.compile(r"(?<![a-z0-9])gc(?![a-z0-9])|序列统计|碱基统计", re.I)
_OTHER_QUANTITY = re.compile(r"长度|位置|坐标|耗时|用时|执行时间|温度")
_ORF = re.compile(r"(?<![a-z0-9])orf(?![a-z0-9])|开放阅读框", re.I)
_RESULT = re.compile(r"结果(?:展示|怎么排|如何排|排序|顺序)|展示(?:结果|记录)|分析结果|统计结果|输出格式")
_FOLLOW_UP = re.compile(r"再做|再对|再跑|再算|刚才|上一次|上一项|上次|之前|照旧|看(?:一下)?结果|解释结果|给(?:我|出)结果")
_PREFERENCE_QUERY = re.compile(r"偏好|默认(?:设置|格式|顺序)|记住了?|长期记忆")


def _gc_decimals_in(text: str) -> int | None:
    decimals = None
    for clause in text.split(","):
        if _OTHER_QUANTITY.search(clause):
            continue
        for match in _PRECISE.finditer(clause):
            if not _NEGATED_FORMAT.search(clause[:match.start()]):
                decimals = _NUMBERS[match[1]]
    return decimals


def _changes_in(text: str, *, gc_topic: bool) -> dict[str, str | int]:
    updates: dict[str, str | int] = {}
    orders = [(match, "file") for match in _FILE_ORDER.finditer(text)]
    orders.extend(
        (match, "sequence_id") for match in _ID_ORDER.finditer(text)
        if not re.match(r"\s*(?:的\s*)?(?:降序|倒序|从大到小|从高到低|desc\b)", text[match.end():], re.I)
    )
    for match, value in sorted(orders, key=lambda item: item[0].start()):
        if not _NEGATED_FORMAT.search(text[:match.start()]):
            updates[RESULT_ORDER] = value

    if gc_topic and (decimals := _gc_decimals_in(text)) is not None:
        updates[GC_DECIMALS] = decimals
    return updates


def remembered_output_preferences(text: str) -> dict[str, str | int]:
    """Extract only supported changes with an explicit lasting scope."""
    text = unicodedata.normalize("NFKC", text)
    if _READ_BACK.search(text.lstrip()):
        return {}

    updates: dict[str, str | int] = {}
    for sentence in re.split(r"[。；;！？!?\n\r]+", text):
        if _NO_SAVE.search(sentence):
            continue
        scopes = list(_SCOPE.finditer(sentence))
        if not scopes:
            continue

        gc_topic = bool(_GC.search(sentence))
        if _DEFAULT_SUFFIX.search(sentence) and all(match.lastgroup == "durable" for match in scopes):
            updates.update(_changes_in(sentence, gc_topic=gc_topic))

        for index, scope in enumerate(scopes):
            if scope.lastgroup == "example":
                break
            if scope.lastgroup != "durable":
                continue
            end = scopes[index + 1].start() if index + 1 < len(scopes) else len(sentence)
            updates.update(_changes_in(sentence[scope.end():end], gc_topic=gc_topic))

    return updates


def requested_gc_decimals(text: str) -> int | None:
    """Read the display precision for the current request."""
    text = unicodedata.normalize("NFKC", text)
    if re.match(r"^(?:请|能否|能不能)?举", text.lstrip()):
        return None
    decimals = None
    for sentence in re.split(r"[。；;！？!?\n\r]+", text):
        if _DEFAULT_SUFFIX.search(sentence):
            continue
        scopes = list(_SCOPE.finditer(sentence))
        current_parts = [sentence[:scopes[0].start()]] if scopes else [sentence]
        for index, scope in enumerate(scopes):
            if scope.lastgroup == "example":
                break
            if scope.lastgroup == "temporary" and not scope[0].startswith("下"):
                end = scopes[index + 1].start() if index + 1 < len(scopes) else len(sentence)
                current_parts.append(sentence[scope.end():end])
        for part in current_parts:
            if (places := _gc_decimals_in(part)) is not None:
                decimals = places
    return decimals


def relevant_output_preferences(
    question: str, analysis_types: set[str], recent_analysis: str | None = None,
) -> set[str]:
    """Use the actual analysis type when known, including references to older jobs."""
    question = unicodedata.normalize("NFKC", question)
    if _PREFERENCE_QUERY.search(question):
        return {RESULT_ORDER, GC_DECIMALS}

    types = analysis_types.copy()
    if _GC.search(question):
        types.add("seq_stats")
    if _ORF.search(question):
        types.add("find_orf")
    if not types and recent_analysis and _FOLLOW_UP.search(question):
        types.add(recent_analysis)

    fields = set()
    if types or _RESULT.search(question):
        fields.add(RESULT_ORDER)
    if "seq_stats" in types:
        fields.add(GC_DECIMALS)
    return fields
