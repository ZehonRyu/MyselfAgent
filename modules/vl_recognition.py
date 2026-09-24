"""
多模态 VL 视觉识别模块
=======================
职责：
1. 输入截图图像，调用云端多模态VL模型（OpenRouter兼容OpenAI格式）
2. 解析截图中的聊天消息：是否存在新消息、消息文本、发送方
3. 强制输出结构化 JSON，做格式校验，解析失败不崩溃
4. 超时捕获、异常捕获，接口失败时自动暂停流程

输出：VLResult
  - has_new_message: 是否有对方发来的新消息
  - messages: List[Message]
  - raw_response: 原始返回
  - is_valid: JSON校验是否通过
  - error: 错误信息

JEV 模型预留：vl_provider="custom" 时走自定义 URL，
只要兼容 OpenAI chat/completions 格式即可直接使用。
"""
import base64
import json
import re
import requests
from io import BytesIO
from typing import Optional
from PIL import Image

from .models import VLResult, Message, Rect
from .logger import log


# VL 识别提示词（通用聊天软件，要求模型输出固定 JSON）
VL_SYSTEM_PROMPT = """你是一个通用 PC 聊天软件界面分析助手。请分析这张聊天窗口截图，识别其中的聊天消息。

支持但不限于：微信、QQ、钉钉、飞书、Telegram 等常见聊天软件。

请严格以JSON格式输出，格式如下：
{
  "has_new_message": true或false,
  "input_box": {"x": 100, "y": 800, "width": 300, "height": 40},
  "messages": [
    {"text": "消息内容", "sender": "self"}
  ]
}

规则：
1. has_new_message: 是否有对方发来的、需要我方回复的新消息（最新一条是对方发的）
2. input_box: 聊天输入框的矩形区域（用于输入回复文本的位置），坐标为截图内的像素坐标
   - x, y 为矩形左上角坐标，width, height 为宽高
   - 若找不到输入框，返回 null
3. messages: 按从上到下顺序列出可见的聊天消息，最近的消息放在最后
4. sender: "self"=我方发送的消息气泡，"other"=对方发送的消息气泡
   （不同软件气泡颜色/位置可能不同，我方消息通常靠右，对方靠左）
5. 只输出JSON，不要输出任何其他文字、解释或markdown标记
6. 如果无法识别消息，返回空列表：{"has_new_message": false, "input_box": null, "messages": []}
7. 忽略系统消息、时间分隔、表情包占位等非对话内容"""


# 联系人导航提示词（用于按提示词点击目标联系人）
NAV_SYSTEM_PROMPT = """你是一个通用 PC 聊天软件界面分析助手。给定一张聊天软件全屏截图和用户的"导航目标"，请在联系人/会话列表中找到该目标对应的联系人项，并返回其矩形区域。

支持但不限于：微信、QQ、钉钉、飞书、Telegram 等常见聊天软件（联系人列表通常在左侧或顶部）。

严格以JSON输出，格式如下：
{
  "found": true,
  "target": {"x": 120, "y": 340, "width": 200, "height": 48},
  "reason": "匹配到联系人 daliu"
}

规则：
1. target 为目标联系人项在截图中的矩形（像素坐标），x,y 为左上角，width,height 为宽高
2. 坐标必须精确覆盖该联系人项的可点击区域（整行）
3. 找不到则返回 {"found": false, "reason": "..."}
4. 只输出JSON，不要输出任何其他文字或markdown标记"""


class VLRecognition:
    """多模态VL视觉识别模块"""

    def __init__(self, api_url: str, api_key: str, model_name: str,
                 timeout: int = 30, provider: str = "openrouter"):
        self._api_url = api_url
        self._api_key = api_key
        self._model = model_name
        self._timeout = timeout
        self._provider = provider

    def update_config(self, api_url: str = None, api_key: str = None,
                      model_name: str = None, timeout: int = None,
                      provider: str = None):
        if api_url is not None:
            self._api_url = api_url
        if api_key is not None:
            self._api_key = api_key
        if model_name is not None:
            self._model = model_name
        if timeout is not None:
            self._timeout = timeout
        if provider is not None:
            self._provider = provider

    # ================================================================
    #  图片转 base64
    # ================================================================
    @staticmethod
    def _image_to_base64(img: Image.Image, max_size: int = 1024) -> str:
        """PIL.Image 转 base64，自动压缩到 max_size 以内控制 token 成本"""
        # 等比缩放
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buffered = BytesIO()
        img.save(buffered, format="JPEG", quality=85)
        b64 = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"

    # ================================================================
    #  核心识别方法
    # ================================================================
    def recognize(self, img: Image.Image) -> VLResult:
        """
        输入截图，返回 VLResult

        异常不抛出，全部捕获写入 VLResult.error
        """
        if not self._api_url or not self._api_key:
            return VLResult(
                is_valid=False,
                error="VL API URL 或 Key 未配置",
            )

        try:
            img_b64 = self._image_to_base64(img)
            log.debug(f"图片转base64完成，准备调用VL模型: {self._model}")

            payload = {
                "model": self._model,
                "messages": [
                    {
                        "role": "system",
                        "content": VL_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "请分析这张聊天窗口截图，输出JSON。",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": img_b64},
                            },
                        ],
                    },
                ],
                "max_tokens": 1000,
                "temperature": 0.3,
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
            # OpenRouter 额外 header
            if self._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "WeChatAssistant"

            log.info(f"调用VL模型: {self._model}")
            resp = requests.post(
                self._api_url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            # 提取返回文本
            raw_text = data["choices"][0]["message"]["content"]
            log.debug(f"VL原始返回: {raw_text[:200]}...")

            # 解析 JSON
            return self._parse_response(raw_text)

        except requests.exceptions.Timeout:
            msg = f"VL请求超时({self._timeout}s)"
            log.error(msg)
            return VLResult(is_valid=False, error=msg)

        except requests.exceptions.ConnectionError as e:
            msg = f"VL连接失败: {e}"
            log.error(msg)
            return VLResult(is_valid=False, error=msg)

        except requests.exceptions.HTTPError as e:
            msg = f"VL HTTP错误: {e} - {resp.text[:200]}"
            log.error(msg)
            return VLResult(is_valid=False, error=msg)

        except (KeyError, IndexError) as e:
            msg = f"VL返回格式异常: {e}"
            log.error(msg)
            return VLResult(is_valid=False, error=msg)

        except Exception as e:
            msg = f"VL识别未知异常: {e}"
            log.error(msg)
            return VLResult(is_valid=False, error=msg)

    # ================================================================
    #  联系人导航：在全屏截图中找到目标联系人矩形（图像坐标，1:1 屏幕）
    # ================================================================
    def find_contact(self, img: Image.Image, goal: str):
        """在截图中找到目标联系人/会话项的矩形，找不到返回 None

        注意：传入的 img 应为全屏截图（不缩放），返回坐标与图像像素 1:1。
        """
        if not self._api_url or not self._api_key:
            return None
        try:
            # 全屏图直接编码为 JPEG（不缩放，保持坐标 1:1）
            buffered = BytesIO()
            img.save(buffered, format="JPEG", quality=85)
            b64 = base64.b64encode(buffered.getvalue()).decode()
            img_url = f"data:image/jpeg;base64,{b64}"

            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": NAV_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": f"导航目标：{goal}\n请在联系人/会话列表中找到该目标对应的联系人项并返回其矩形。"},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ]},
                ],
                "max_tokens": 300,
                "temperature": 0.1,
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
            if self._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "WeChatAssistant"

            log.info(f"调用VL导航: {self._model}, 目标={goal}")
            resp = requests.post(
                self._api_url, headers=headers,
                json=payload, timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            log.debug(f"VL导航原始返回: {raw_text[:200]}...")

            json_str = self._extract_json(raw_text)
            if json_str is None:
                log.warning("VL导航返回无法提取JSON")
                return None
            obj = json.loads(json_str)
            if not obj.get("found"):
                log.warning(f"VL导航未找到目标联系人: {obj.get('reason', '')}")
                return None

            t = obj.get("target")
            if not isinstance(t, dict):
                return None
            try:
                x = int(t.get("x", 0)); y = int(t.get("y", 0))
                w = int(t.get("width", 0)); h = int(t.get("height", 0))
            except (TypeError, ValueError):
                return None
            if w <= 0 or h <= 0:
                return None
            log.info(f"VL导航找到目标: ({x},{y}) {w}x{h}")
            return Rect(x, y, w, h)

        except Exception as e:
            log.error(f"VL导航异常: {e}")
            return None

    # ================================================================
    #  JSON 解析与校验
    # ================================================================
    def _parse_response(self, raw_text: str) -> VLResult:
        """解析 VL 返回的 JSON，做格式校验"""
        # 尝试提取 JSON（模型可能返回带 markdown 标记的文本）
        json_str = self._extract_json(raw_text)
        if json_str is None:
            log.error(f"VL返回无法提取JSON: {raw_text[:200]}")
            return VLResult(
                is_valid=False,
                error="VL返回无法提取JSON",
                raw_response=raw_text,
            )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            log.error(f"VL返回JSON解析失败: {e}")
            return VLResult(
                is_valid=False,
                error=f"JSON解析失败: {e}",
                raw_response=raw_text,
            )

        # 校验字段
        has_new = data.get("has_new_message", False)
        if not isinstance(has_new, bool):
            has_new = bool(has_new)

        messages = []
        msg_list = data.get("messages", [])
        if isinstance(msg_list, list):
            for m in msg_list:
                if not isinstance(m, dict):
                    continue
                text = m.get("text", "")
                sender = m.get("sender", "other")
                if sender not in ("self", "other"):
                    sender = "other"
                if text:
                    messages.append(Message(text=text, sender=sender))

        # 解析输入框矩形（图像坐标系，用于键鼠发送回复）
        input_box = None
        box = data.get("input_box")
        if isinstance(box, dict):
            try:
                bx = int(box.get("x", 0))
                by = int(box.get("y", 0))
                bw = int(box.get("width", 0))
                bh = int(box.get("height", 0))
                if bw > 0 and bh > 0:
                    input_box = Rect(bx, by, bw, bh)
            except (TypeError, ValueError):
                input_box = None

        log.info(f"VL识别完成: {len(messages)}条消息, "
                 f"has_new={has_new}, input_box={'有' if input_box else '无'}")

        return VLResult(
            has_new_message=has_new,
            messages=messages,
            input_box=input_box,
            raw_response=raw_text,
            is_valid=True,
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从可能包含markdown标记的文本中提取JSON字符串"""
        # 尝试直接解析
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 块
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            candidate = match.group(1).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

        # 尝试提取第一个 { ... } 块
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            candidate = match.group(0).strip()
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass

        return None
