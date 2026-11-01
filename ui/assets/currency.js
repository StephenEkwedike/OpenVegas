// Display only; ledger and request amounts keep their original precision.
export function money(value) {
  if (!/^-?\d{1,15}(\.\d{1,6})?$/.test(String(value))) return 'unavailable';
  const negative = String(value).startsWith('-');
  const [whole, fraction = ''] = String(value).replace(/^-/, '').split('.');
  const cents = BigInt(whole) * 100n + BigInt((fraction + '00').slice(0, 2)) +
    (Number(fraction[2] || 0) >= 5 ? 1n : 0n);
  return '$V ' + (negative && cents ? '-' : '') +
    (cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' +
    (cents % 100n).toString().padStart(2, '0');
}
