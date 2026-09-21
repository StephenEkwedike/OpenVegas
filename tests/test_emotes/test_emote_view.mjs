import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../../ui/assets/emote-view.js", import.meta.url), "utf8");
const { catalogItems, matchingItems, previewPath, danceSpec, isPurchasable, previewFrame, collectionStatus, equipmentRequest, plannedPriceLabel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const item = { id: "openvegas.test", name: "Test", category: "companion", description: "Dance", cost_v: "2.5", purchasable: true, preview_only: false };
assert.equal(catalogItems({items: [item, item, {...item, id: "<script>"}]}).length, 1);
assert.equal(matchingItems([item], "DANCE", "owned", new Map([[item.id, true]])).length, 1);
assert.equal(matchingItems([item], "", "owned", new Map()).length, 0);
assert.equal(previewPath("/ui/assets/emotes/test/sheet.png"), "/ui/assets/emotes/test/sheet.png");
for (const value of ["https://evil.test/a.png", "/ui/assets/emotes/../secret", "//evil.test/a.png", "/ui/assets/emotes/%2e%2e/secret"]) assert.equal(previewPath(value), null);
assert.equal(isPurchasable(item), true);
assert.equal(isPurchasable({...item, preview_only: true}), false);
assert.equal(isPurchasable({...item, cost_v: "NaN"}), false);
const spec = {schema_version: 1, frame: {width: 64, height: 80}, animations: {waiting: {frames: [0,1,2,3], frame_ms: 140}}};
assert.equal(danceSpec(spec).frames.length, 4);
assert.throws(() => danceSpec({...spec, frame: {width: 0, height: 80}}));
assert.throws(() => danceSpec({...spec, animations: {waiting: {frames: [-1], frame_ms: 140}}}));
console.log("Emotes view: catalog, filters, paths, purchase gates, manifest bounds passed");

const waiting = danceSpec(spec);
assert.equal(previewFrame(waiting, 4 * 140), 0);
const completion = danceSpec({...spec, animations:{complete:{frames:[0,1,2,3],frame_ms:140,loop:true}}}, "complete");
assert.equal(previewFrame(completion, 10000), 3);
assert.equal(previewFrame(completion, -100), 0);
assert.equal(previewFrame(completion, NaN), 0);
assert.equal(previewFrame(completion, 10000, true), 0);
assert.equal(previewFrame(completion, 0), 0);
assert.match(collectionStatus([]), /Sales remain closed/);
assert.match(collectionStatus([item]), /1 packs available/);
console.log("PASS once-only completion, replay start, reduced motion and dynamic sale status");

assert.deepEqual(equipmentRequest(item), {item_id:item.id,slot:"companion"});
assert.deepEqual(equipmentRequest({...item,category:"completion"}), {item_id:item.id,slot:"completion"});
assert.throws(() => equipmentRequest({...item,category:"other"}));
console.log("PASS correct companion/completion equipment payloads");

assert.equal(plannedPriceLabel({planned_cost_v:"500.000000",planned_cost_usd:"5.00"}), "Planned price: 500 $V (US$5.00), one-time. Not yet for sale.");
assert.equal(plannedPriceLabel({...item,planned_cost_v:"500",planned_cost_usd:"5.00"}), "");
assert.equal(plannedPriceLabel({planned_cost_v:"<script>",planned_cost_usd:"5.00"}), "");
assert.equal(plannedPriceLabel({}), "");
console.log("PASS planned pricing never enables purchases");
