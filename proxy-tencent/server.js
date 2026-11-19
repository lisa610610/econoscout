/**
 * EconoScout AI 问答代理（腾讯云 SCF Web 函数）
 * ------------------------------------------------------------------
 * 与 Cloudflare 版功能一致，但跑在国内腾讯云，函数 URL 国内直连。
 * Key 只存在 SCF 的环境变量里，前端永远碰不到，F12 搜 Key 为 0。
 *
 * 部署要点（详见 部署指南.md）：
 *  - 函数类型：Web 函数
 *  - 运行环境：Node.js 18
 *  - 启动命令：node server.js
 *  - 监听端口：9000（绑定 0.0.0.0）
 *  - 环境变量：DEEPSEEK_API_KEY、ALLOWED_ORIGIN
 *  - 函数 URL：开启公网访问、授权类型选「开放」
 * ------------------------------------------------------------------
 */
const http = require("http");

const PORT = process.env.PORT || 9000;
const DEEPSEEK_API_KEY = (process.env.DEEPSEEK_API_KEY || "").trim();
const DEEPSEEK_MODEL = (process.env.DEEPSEEK_MODEL || "deepseek-chat").trim();
const ALLOWED_ORIGIN = (process.env.ALLOWED_ORIGIN || "https://lisa610610.github.io").trim();

function cors(res) {
  res.setHeader("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Access-Control-Max-Age", "86400");
  res.setHeader("Vary", "Origin");
}

function sendJson(res, status, obj) {
  cors(res);
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", c => {
      data += c;
      if (data.length > 200000) { reject(new Error("请求过大")); req.destroy(); }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  const origin = req.headers.origin || "";

  // CORS 预检
  if (req.method === "OPTIONS") {
    cors(res); res.statusCode = 204; res.end(); return;
  }
  if (req.method !== "POST") {
    return sendJson(res, 405, { error: "仅支持 POST 请求" });
  }
  // 来源白名单（本地调试也放行）
  const isLocal = origin.startsWith("http://localhost") || origin.startsWith("http://127.0.0.1");
  if (origin && origin !== ALLOWED_ORIGIN && !isLocal) {
    return sendJson(res, 403, { error: "来源不被允许" });
  }
  if (!DEEPSEEK_API_KEY) {
    return sendJson(res, 500, { error: "服务端未配置 DEEPSEEK_API_KEY" });
  }

  let body;
  try {
    body = JSON.parse(await readBody(req) || "{}");
  } catch {
    return sendJson(res, 400, { error: "请求格式错误" });
  }
  const messages = Array.isArray(body.messages) ? body.messages : [];
  if (!messages.length) {
    return sendJson(res, 400, { error: "消息不能为空" });
  }
  const safeMessages = messages.slice(-12).map(m => ({
    role: ["system", "user", "assistant"].includes(m.role) ? m.role : "user",
    content: String(m.content || "").slice(0, 4000),
  }));

  try {
    // 调 DeepSeek，拿流式响应
    const upstream = await fetch("https://api.deepseek.com/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + DEEPSEEK_API_KEY,
      },
      body: JSON.stringify({
        model: DEEPSEEK_MODEL,
        messages: safeMessages,
        temperature: 0.3,
        max_tokens: 800,
        stream: true,
      }),
    });
    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => "");
      return sendJson(res, 502, { error: "上游模型服务错误", detail: detail.slice(0, 300) });
    }

    // 以 SSE 流式转发给前端：data: {"content":"增量"}
    cors(res);
    res.statusCode = 200;
    res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("X-Accel-Buffering", "no");

    const reader = upstream.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const sse = obj => "data: " + JSON.stringify(obj) + "\n\n";

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
          if (delta) res.write(sse({ content: delta }));
        } catch { /* 忽略解析噪声 */ }
      }
    }
    res.write("data: [DONE]\n\n");
    res.end();
  } catch (e) {
    if (!res.headersSent) {
      sendJson(res, 502, { error: "请求模型失败：" + (e.message || "未知错误") });
    } else {
      res.end();
    }
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log("[EconoScout] SCF Web 函数已启动，监听 " + PORT);
  console.log("[EconoScout] ALLOWED_ORIGIN = " + ALLOWED_ORIGIN);
  console.log("[EconoScout] DeepSeek Key " + (DEEPSEEK_API_KEY ? "已配置" : "未配置"));
});
