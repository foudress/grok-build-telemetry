import assert from "node:assert/strict";
import { renderRoundTree } from "../dashboard/js/tree.js";

const tree = { querySelectorAll: () => [], scrollTop: 0, innerHTML: "" };
globalThis.document = { getElementById: (id) => id === "roundTree" ? tree : null };
globalThis.window = {};

renderRoundTree([
  { index: 1, user_prompt: { tokens_in: 12 } },
  {
    index: 2,
    user_prompt: {
      tokens_in: 23,
      prompt_tokens_in: 18,
      prev_llm_answer: { round_index: 1, tokens_out: 8, tokens_in: 5, tokens_cached: 3 },
    },
  },
  {
    index: 3,
    user_prompt: {
      tokens_in: 4,
      prev_llm_answer: { round_index: 2, tokens_out: 35, tokens_in: 0 },
    },
  },
]);

function userRow(round) {
  const start = tree.innerHTML.indexOf(`>User [${round}]<`);
  assert.notEqual(start, -1);
  return tree.innerHTML.slice(start, tree.innerHTML.indexOf("</details>", start));
}

assert.doesNotMatch(userRow(1), /LLM answer round/);
assert.match(userRow(2), /LLM answer round \[1\][\s\S]*In \+5[\s\S]*Cached 3[\s\S]*>prompt<[\s\S]*In \+18/);
assert.match(userRow(3), /LLM answer round \[2\][\s\S]*Previous Out 35/);
assert.doesNotMatch(userRow(3), /In \+35/);
