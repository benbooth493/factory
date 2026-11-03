export const meta = {
  name: 'factory-board-wave',
  description: 'Run one factory wave: each claimed card goes Build → QA → Review until it stops advancing',
  whenToUse: 'Launched by /factory:board run with args the orchestrator prepared. Not for direct use.',
  phases: [
    { title: 'Build', detail: 'Build lane: branch, draft PR, passing checks' },
    { title: 'QA', detail: 'QA lane: evidence for each acceptance criterion' },
    { title: 'Review', detail: 'Review lane: independent review, merge gate' },
  ],
}

// args = {
//   repo: 'owner/name',
//   pluginRoot: '/abs/path/to/factory/<version>',
//   cards: [{
//     number, title,
//     lane: 'build' | 'qa' | 'review',   // the lane the card enters at (already claimed for it)
//     grade: 'easy' | 'medium' | 'hard',
//     models: { build, qa, review },     // from route.py model; 'inherit' or missing = session model
//   }],
// }
// The orchestrator claims each card for its entry lane before launching. A lane
// releases the claim when it finishes, so every later lane in the chain claims
// the card again first: the factory:wip label stays the only mutex.
// The board is the state. This script decides nothing a lane did not report;
// a card that stops advancing is picked up by the next wave from its column.
const input = (() => {
  if (typeof args !== 'string') return args
  try { return JSON.parse(args) } catch { return args }
})()
if (!input || !input.repo || !input.pluginRoot || !Array.isArray(input.cards)) {
  throw new Error('factory-board-wave needs args {repo, pluginRoot, cards:[{number,title,lane,grade,models}]}')
}

const LANES = ['build', 'qa', 'review']
const PHASE = { build: 'Build', qa: 'QA', review: 'Review' }
// The outcome word in each lane's RESULT line that hands the card to the next lane.
const NEXT = { build: 'qa', qa: 'review' }

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    result: { type: 'string', description: 'the single RESULT line the lane skill asks for, verbatim' },
    outcome: {
      type: 'string',
      enum: ['qa', 'review', 'done', 'build', 'waiting', 'blocked', 'failed'],
      description: 'the outcome word from the RESULT line; failed = you could not finish the lane',
    },
    released: { type: 'boolean', description: 'true when you ran board.sh release for this card' },
    pr: { type: 'string' },
    sha: { type: 'string' },
  },
  required: ['result', 'outcome', 'released'],
}

const B = `${input.pluginRoot}/scripts/board.sh`

const lanePrompt = (lane, card, claimFirst) => [
  `You are the factory **${lane}** lane for issue #${card.number} ("${card.title}") in ${input.repo}.`,
  `CLAUDE_PLUGIN_ROOT is ${input.pluginRoot}; wherever the skill says \${CLAUDE_PLUGIN_ROOT}, use that path.`,
  claimFirst
    ? `The previous lane released the card. Claim it first from the repo root: \`${B} claim ${card.number}\`. ` +
      `If the claim fails, change nothing and return outcome failed with result "RESULT: ${lane} #${card.number} failed reason=\\"claim held\\"".`
    : `The card is already claimed for you (factory:wip).`,
  `Read \`${input.pluginRoot}/skills/${lane}/SKILL.md\` and follow it exactly. Config is \`.claude/factory.json\`.`,
  `Card grade: ${card.grade || 'medium'}. Put \`Grade: ${card.grade || 'medium'}\` in a Build hand-off comment.`,
  `Return the RESULT line the skill asks for, verbatim, plus its outcome word, through structured output.`,
].join('\n')

const modelFor = (card, lane) => {
  const m = card.models && card.models[lane]
  return m && m !== 'inherit' ? m : undefined
}

const runCard = async (card) => {
  if (!LANES.includes(card.lane)) {
    return { number: card.number, history: [{ lane: card.lane, outcome: 'failed', result: `unknown lane ${card.lane}`, released: false }] }
  }
  const history = []
  let lane = card.lane
  while (lane) {
    const model = modelFor(card, lane)
    const r = await agent(lanePrompt(lane, card, history.length > 0), {
      label: `${lane}:#${card.number}`,
      phase: PHASE[lane],
      schema: RESULT_SCHEMA,
      ...(model ? { model } : {}),
    })
    const step = r || { result: `RESULT: ${lane} #${card.number} failed reason="lane returned nothing"`, outcome: 'failed', released: false }
    history.push({ lane, ...step })
    log(`#${card.number} ${step.result}`)
    // Only a hand-off to the next lane keeps the chain going. A bounce to Build
    // ends it here: the orchestrator applies the bounce and same-lane rules
    // before any card is built again.
    lane = step.outcome === NEXT[lane] ? NEXT[lane] : null
  }
  return { number: card.number, history }
}

const results = await parallel(input.cards.map((card) => () => runCard(card)))

const cards = input.cards.map((card, i) => {
  const r = results[i] || { number: card.number, history: [] }
  const last = r.history[r.history.length - 1] ||
    { lane: card.lane, outcome: 'failed', result: 'lane chain died', released: false }
  return {
    number: card.number,
    lanes: r.history.map((h) => `${h.lane}:${h.outcome}`).join(' → ') || 'none',
    lastLane: last.lane,
    outcome: last.outcome,
    result: last.result,
    released: Boolean(last.released),
    pr: last.pr || null,
    sha: last.sha || null,
  }
})
log(`wave done: ${cards.map((c) => `#${c.number} ${c.lanes}`).join(' · ')}`)
return { cards }
