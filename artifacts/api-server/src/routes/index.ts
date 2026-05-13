import { Router, type IRouter } from "express";
import healthRouter from "./health";
import sheetsRouter from "./sheets";
import smsRouter from "./sms";

const router: IRouter = Router();

router.use(healthRouter);
router.use(sheetsRouter);
router.use(smsRouter);

export default router;
