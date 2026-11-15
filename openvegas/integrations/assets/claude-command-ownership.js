// Inlined into both Claude plugins. Only a confirmed empty slot may be claimed.
async function nativeCommandOwner($, name) {
  try {
    const matches = (await $.command.list()).filter(row => row.name === name);
    if (!matches.length) return null;
    if (matches.length === 1 && matches[0].source === 'plugin') {
      const owner = matches[0].plugin;
      if (owner === 'openvegas' || (name === 'openvegas' && owner === 'openvegas-generation')) return owner;
    }
  } catch { /* An unreadable registry is not evidence of an empty slot. */ }
  return false;
}

async function offerNativeCommand($, spec) {
  const owner = await nativeCommandOwner($, spec.name);
  if (owner) return;
  if (owner === null) {
    try {
      await $.command.register(spec);
      return;
    } catch {
      // Another activation may have won since list(). Never replace or retry.
      if (await nativeCommandOwner($, spec.name)) return;
    }
  }
  $.ui.log('OpenVegas did not register /' + spec.name + '; existing commands are unchanged.');
}
