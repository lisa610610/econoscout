/**
 * EconoScout AI 问答代理（Cloudflare Workers）
 * ------------------------------------------------------------------
 * 作用：把前端的问答请求转发给 DeepSeek，API Key 只存在 Worker 的
 *       加密环境变量里，前端永远接触不到，F12 搜 Key 也是 0 处。
 *
 * 部署：在 Cloudflare Workers 网页编辑器里新建 Worker，把本文件全部
 *       内容粘贴进去；再到 Settings → Variables 添加环境变量：
 *         DEEPSEEK_API_KEY  = 你的 DeepSeek Key（sk- 开头）
 *         ALLOWED_ORIGIN    = https://lisa610610.github.io
 *       （可选）DEEPSEEK_MODEL = deepseek-chat
 *       保存部署后即可得到形如 https://xxx.workers.dev 的地址。
 * ------------------------------------------------------------------
 */

// 允许跨域访问的前端来源（白名单，防止别人盗用你的 Key 刷接口）
const DEFAULT_ALLOWED_ORIGIN = "https://lisa610610.github.io";

export default {
  async fetch(request, env) {
    // 允许的来源：优先读环境变量，没配就用默认值
    const allowedOrigin = (env.ALLOWED_ORIGIN || DEFAULT_ALLOWED_ORIGIN).trim();
    const origin = request.headers.get("Origin") || "";

    // 处理 CORS 预检请求
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(allowedOrigin) });
    }

    // 只接受 POST
    if (request.method !== "POST") {
      return json({ error: "仅支持 POST 请求" }, 405, allowedOrigin);
    }

    // 校验来源：必须是白名单里的域名（本地 localhost 也放行，方便调试）
    const isLocal = origin.startsWith("http://localhost") || origin.startsWith("http://127.0.0.1");
    if (origin && origin !== allowedOrigin && !isLocal) {
      return json({ error: "来源不被允许" }, 403, allowedOrigin);
    }

    const key = (env.DEEPSEEK_API_KEY || "").trim();
    if (!key) {
      return json({ error: "服务端未配置 DEEPSEEK_API_KEY" }, 500, allowedOrigin);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "请求格式错误" }, 400, allowedOrigin);
    }

    const messages = Array.isArray(body.messages) ? body.messages : [];
    if (messages.length === 0) {
      return json({ error: "消息不能为空" }, 400, allowedOrigin);
    }

    // 简单限流保护：限制条数和单条长度，避免被当免费 tokens 刷
    const safeMessages = messages.slice(-12).map(m => ({
      role: ["system", "user", "assistant"].includes(m.role) ? m.role : "user",
      content: String(m.content || "").slice(0, 4000),
    }));
    const model = (env.DEEPSEEK_MODEL || "deepseek-chat").trim();

    try {
      // 调用 DeepSeek（OpenAI 兼容格式）
      const upstream = await fetch("https://api.deepseek.com/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + key,
        },
        body: JSON.stringify({
          model,
          messages: safeMessages,
          temperature: 0.3,
          max_tokens: 800,
          stream: true,
        }),
      });

      if (!upstream.ok) {
        const detail = await upstream.text().catch(() => "");
        return json({ error: "上游模型服务错误", detail: detail.slice(0, 300) }, 502, allowedOrigin);
      }

      // 把 DeepSeek 的 SSE 流转成简化流：每行 data: {"content":"增量文本"}
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          const reader = upstream.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split("\n");
              buffer = lines.pop() || "";
              for (const line of lines) {
                const t = line.trim();
                if (!t.startsWith("data:")) continue;
                const data = t.slice(5).trim();
                if (data === "[DONE]") continue;
                try {
                  const parsed = JSON.parse(data);
                  const delta = parsed.choices?.[0]?.delta?.content || "";
                  if (delta) {
                    controller.enqueue(encoder.encode(encodeSSE({ content: delta })));
                  }
                } catch { /* 忽略无法解析的行 */ }
              }
            }
          } catch (e) {
            controller.enqueue(encoder.encode(encodeSSE({ error: "流中断" })));
          } finally {
            controller.enqueue(encoder.encode("data: [DONE]\n\n"));
            controller.close();
          }
        },
      });

      return new Response(stream, {
        headers: {
          ...corsHeaders(allowedOrigin),
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-cache",
          "X-Accel-Buffering": "no",
        },
      });
    } catch (e) {
      return json({ error: "请求模型失败：" + (e.message || "未知错误") }, 502, allowedOrigin);
    }
  },
};

function corsHeaders(allowedOrigin) {
  return {
    "Access-Control-Allow-Origin": allowedOrigin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function encodeSSE(obj) {
  return "data: " + JSON.stringify(obj) + "\n\n";
}

function json(obj, status, allowedOrigin) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders(allowedOrigin), "Content-Type": "application/json; charset=utf-8" },
  });
}
