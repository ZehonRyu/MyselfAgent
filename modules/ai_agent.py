"""
AI Agent 调用模块
==================
职责：
1. 接收对话上下文（历史消息 + 最新消息），调用云端AI Agent生成回复
2. 将历史 Message 列表转为 OpenAI messages 格式
3. 超时捕获、异常捕获，接口失败不崩溃，返回 AgentResult
4. JEV 预留：agent_provider="custom" 时走自定义 URL

输出：AgentResult
  - reply_text: 生成的回复文本
  - success: 是否成功
  - error: 错误信息
"""
import requests
from typing import List

from .models import Message, AgentResult
from .logger import log


# AI Agent 系统提示词（通用聊天软件）
AGENT_SYSTEM_PROMPT = """你是一个通用聊天对话回复助手。请根据以下聊天记录，生成我方的回复。

要求：
1. 语气自然、口语化，像真人打字
2. 回复简短，一般1-2句话，不要长篇大论
3. 不要使用markdown格式、不要用列表
4. 直接输出回复内容，不要加"回复："等前缀
5. 根据上下文理解对方意图，给出合理的回复"""


class AIAgent:
    """AI Agent 调用模块"""

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
    #  消息列表转 OpenAI 格式
    # ================================================================
    @staticmethod
    def _build_messages(history: List[Message]) -> list:
        """将 Message 列表转为 OpenAI chat messages 格式

        对方消息 -> user 角色
        我方消息 -> assistant 角色
        """
        messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]

        for msg in history:
            if msg.sender == "self":
                messages.append({"role": "assistant", "content": msg.text})
            else:
                messages.append({"role": "user", "content": msg.text})

        return messages

    # ================================================================
    #  核心调用方法
    # ================================================================
    def generate_reply(self, history: List[Message]) -> AgentResult:
        """
        输入对话历史，生成回复文本

        异常不抛出，全部捕获写入 AgentResult.error
        """
        if not self._api_url or not self._api_key:
            return AgentResult(error="Agent API URL 或 Key 未配置")

        if not history:
            return AgentResult(error="对话历史为空")

        try:
            messages = self._build_messages(history)
            log.debug(f"Agent上下文: {len(messages)}条消息")

            payload = {
                "model": self._model,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0.8,
            }

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
            if self._provider == "openrouter":
                headers["HTTP-Referer"] = "http://localhost"
                headers["X-Title"] = "WeChatAssistant"

            log.info(f"调用AI Agent: {self._model}")
            resp = requests.post(
                self._api_url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            reply = data["choices"][0]["message"]["content"].strip()
            log.info(f"Agent回复: {reply}")

            return AgentResult(
                reply_text=reply,
                success=True,
                raw_response=reply,
            )

        except requests.exceptions.Timeout:
            msg = f"Agent请求超时({self._timeout}s)"
            log.error(msg)
            return AgentResult(error=msg)

        except requests.exceptions.ConnectionError as e:
            msg = f"Agent连接失败: {e}"
            log.error(msg)
            return AgentResult(error=msg)

        except requests.exceptions.HTTPError as e:
            err_text = resp.text[:200] if resp else ""
            msg = f"Agent HTTP错误: {e} - {err_text}"
            log.error(msg)
            return AgentResult(error=msg)

        except (KeyError, IndexError) as e:
            msg = f"Agent返回格式异常: {e}"
            log.error(msg)
            return AgentResult(error=msg)

        except Exception as e:
            msg = f"Agent调用未知异常: {e}"
            log.error(msg)
            return AgentResult(error=msg)
