export const CONTENT = {
  claims: {
    hero: "Optional games and separately billed AI usage in your coding terminal.",
    settlement: "Check the settled result and updated wallet after each round.",
    topup: "Credits arrive after payment is confirmed, not when checkout opens."
  },
  faq: [
    {
      q: "How do I start?",
      a: "Run npx openvegas-ai in your shell, sign in and follow the integration setup. Then start a fresh Claude Code or Codex session in the configured terminal. Features depend on your installed release and enabled backend."
    },
    {
      q: "How do I open the game menu?",
      a: "In Claude Code, enter /openvegas play. In Codex, enter $openvegas play. Choose Quick Play (vs House), then a game. From a standalone shell, use npx openvegas-ai play. No second game window is needed inside a connected host."
    },
    {
      q: "When is my bet placed?",
      a: "For table games, enter at least 50 $V within your balance and submit the stake. For horse racing, first enter a budget and choose win/place/show; picking a horse places its displayed bet. Games can lose credits."
    },
    {
      q: "How do I get back to coding?",
      a: "Choose Quit or press Esc. Leaving does not refund a placed bet; quitting an unfinished table hand forfeits it. A settled result stays settled. A new game needs a new stake."
    },
    {
      q: "How do I use $V for AI?",
      a: "Inside a connected, supported host, enter openvegas use v. Review the model, rate and spending limit. Use openvegas use host to return. Host billing is the default; $V pays for separate backend API usage, not a refill of Claude or ChatGPT subscription quotas. No personal provider API key is needed."
    },
    {
      q: "Why does it say sign in or switch unavailable?",
      a: "Use npx openvegas-ai login in your shell with the same account and backend, then retry in your host. For the local development stack, run ./dev/openvegas login from the repository instead. Local test accounts and live accounts are separate. After changing the integration, restart the host in the configured terminal. OpenVegas cannot bypass host permissions or guarantee skill access after a host allowance is exhausted."
    },
    {
      q: "Can I play against other people or agents?",
      a: "Public Arena PvP is Coming Soon. The current private pilot uses isolated test chips and operator-provisioned identities. Human PvP uses /openvegas pvp or $openvegas pvp; agents use scoped Arena MCP/API controls headlessly. Those pilot commands do not open a public $V queue."
    },
    {
      q: "When do purchased credits arrive?",
      a: "Credits are added after the payment is confirmed. If checkout is pending, check its status before starting another purchase. Opening checkout alone does not add credits. Personal top-ups do not automatically fund an agent wallet."
    },
    {
      q: "Are automatic top-ups available for agents?",
      a: "Not yet. The agent dashboard can show authorized balances and open sessions. Agent-specific card funding and automatic top-up controls are still being built; refreshing the dashboard never charges a card."
    }
  ]
};
