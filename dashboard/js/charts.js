/** Context line chart + cost bars / legend — facade (Phase 3 split). */
export {
  buildCtxPoints,
  drawLineChart,
  drawBars,
  drawAggBars,
  renderCostLegend,
  setCostUnit,
  hideAllChartTips,
  hideChartTip,
  storedCtxChartH,
  bindCtxChartResize,
  onGanttSelect,
} from './cost-draw.js';
export { findRound } from './cost-model.js';
export { drawTimeline, fitGanttToSessions } from './gantt.js';
export { drawRateChart, buildSessionRatePoints, clearRateHost } from './rate-chart.js';
export { drawIoStepChart } from './io-step.js';
