import { Router, type IRouter } from "express";
import healthRouter from "./health";
import sheetsRouter from "./sheets";
import smsRouter from "./sms";
import twimlRouter from "./twiml";

const router: IRouter = Router();

router.use(healthRouter);
router.use(sheetsRouter);
router.use(smsRouter);
router.use(twimlRouter);

export default router;
