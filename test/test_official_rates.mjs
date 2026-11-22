import assert from "node:assert/strict";
import {
  implyOfficialRatesPerM,
  normalizeModelFamily,
} from "../dashboard/js/fmt.js";

assert.equal(normalizeModelFamily("grok-4.20"), "grok-4.20");
assert.equal(normalizeModelFamily("grok-4.3"), "grok-4.3");
assert.equal(normalizeModelFamily("grok-4.7"), "grok-4.7");
assert.equal(normalizeModelFamily("grok-4.6"), "grok-4.6");
assert.equal(normalizeModelFamily("grok-4.5"), "grok-4.5");

const tok = { unc: 1e6, cached: 1e6, out: 1e6 };

const card420 = implyOfficialRatesPerM(3.95, tok, { model: "grok-4.20" });
assert.equal(card420.snapped, true);
assert.equal(card420.in, 1.25);
assert.equal(card420.cached, 0.2);
assert.equal(card420.out, 2.5);

const card43 = implyOfficialRatesPerM(3.95, tok, { model: "grok-4.3" });
assert.equal(card43.cached, 0.2);

const card47 = implyOfficialRatesPerM(8.5, tok, { model: "grok-4.7" });
assert.equal(card47.snapped, true);
assert.equal(card47.cached, 0.5);

const card47near45 = implyOfficialRatesPerM(8.3, tok, { model: "grok-4.7" });
assert.notEqual(card47near45.cached, 0.3);

const card45 = implyOfficialRatesPerM(8.3, tok, { model: "grok-4.5" });
assert.equal(card45.snapped, true);
assert.equal(card45.cached, 0.3);

const card46 = implyOfficialRatesPerM(8.5, tok, { model: "grok-4.6" });
assert.equal(card46.snapped, true);
assert.equal(card46.cached, 0.5);

console.log("official rate cards ok");
