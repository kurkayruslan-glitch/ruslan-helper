import { Router, type IRouter } from "express";
import healthRouter from "./health";
import sheetsRouter from "./sheets";

const router: IRouter = Router();

router.use(healthRouter);
router.use(sheetsRouter);

export default router;
