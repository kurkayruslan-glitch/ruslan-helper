import { Router } from "express";

const smsRouter = Router();

smsRouter.get("/sms", (req, res) => {
  const to = String(req.query.to ?? "").replace(/\s|-/g, "");
  const body = String(req.query.body ?? "");
  const smsUri = `sms:${to}?body=${encodeURIComponent(body)}`;

  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.send(`<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0;url=${smsUri}">
  <title>Отправка SMS...</title>
  <style>
    body { font-family: -apple-system, sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; background:#f0f0f0; }
    .box { text-align:center; background:white; padding:32px; border-radius:16px; box-shadow:0 2px 16px rgba(0,0,0,0.1); }
    a { display:inline-block; margin-top:16px; padding:12px 24px; background:#007AFF; color:white; border-radius:8px; text-decoration:none; font-size:16px; }
  </style>
</head>
<body>
  <div class="box">
    <div style="font-size:48px">📱</div>
    <h2>Открываю SMS приложение...</h2>
    <p>Если не открылось автоматически:</p>
    <a href="${smsUri}">Нажми здесь</a>
  </div>
  <script>window.location.href = "${smsUri}";</script>
</body>
</html>`);
});

export default smsRouter;
