import { Router } from "express";
import { ReplitConnectors } from "@replit/connectors-sdk";

const router = Router();
const connectors = new ReplitConnectors();

// GET /api/sheets/:spreadsheetId/values/:range
router.get("/sheets/:spreadsheetId/values/:range", async (req, res) => {
  try {
    const { spreadsheetId, range } = req.params;
    const response = await connectors.proxy(
      "google-sheet",
      `/v4/spreadsheets/${spreadsheetId}/values/${encodeURIComponent(range)}`,
      { method: "GET" }
    );
    const data = await response.json();
    res.json(data);
  } catch (err: any) {
    req.log.error(err);
    res.status(500).json({ error: err.message });
  }
});

// POST /api/sheets/:spreadsheetId/values/:range/append
router.post("/sheets/:spreadsheetId/values/:range/append", async (req, res) => {
  try {
    const { spreadsheetId, range } = req.params;
    const response = await connectors.proxy(
      "google-sheet",
      `/v4/spreadsheets/${spreadsheetId}/values/${encodeURIComponent(range)}:append?valueInputOption=USER_ENTERED`,
      {
        method: "POST",
        body: JSON.stringify(req.body),
        headers: { "Content-Type": "application/json" },
      }
    );
    const data = await response.json();
    res.json(data);
  } catch (err: any) {
    req.log.error(err);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sheets/:spreadsheetId/info
router.get("/sheets/:spreadsheetId/info", async (req, res) => {
  try {
    const { spreadsheetId } = req.params;
    const response = await connectors.proxy(
      "google-sheet",
      `/v4/spreadsheets/${spreadsheetId}?includeGridData=false`,
      { method: "GET" }
    );
    const data = await response.json();
    res.json(data);
  } catch (err: any) {
    req.log.error(err);
    res.status(500).json({ error: err.message });
  }
});

export default router;
