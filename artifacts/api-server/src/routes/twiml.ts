import { Router } from "express";

const twimlRouter = Router();

twimlRouter.get("/twiml", (req, res) => {
  const message = String(req.query.message ?? "Привет, это сообщение от Руслана.");
  const lang = "ru-RU";

  res.setHeader("Content-Type", "text/xml; charset=utf-8");
  res.send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="${lang}" voice="Polly.Tatyana">${message}</Say>
  <Pause length="1"/>
  <Say language="${lang}" voice="Polly.Tatyana">${message}</Say>
</Response>`);
});

export default twimlRouter;
