/* Shared PayPal checkout helper: official JS SDK buttons in sandbox mode, the local simulator otherwise. */
window.BillkindPay = (() => {
  const csrf = () => document.querySelector('meta[name=csrf]').content;
  const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF': csrf()}, body: JSON.stringify(body || {})});
  const loaded = {};
  function loadSdk(clientId, cur) {
    if (!loaded[cur]) loaded[cur] = new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=${cur}&intent=capture&components=buttons&disable-funding=card,credit,paylater,venmo`;
      s.setAttribute('data-namespace', 'pp_' + cur);
      s.onload = () => res(window['pp_' + cur]); s.onerror = rej; document.head.appendChild(s);
    });
    return loaded[cur];
  }
  async function create(getPicks, source, onError) {
    const r = await post('/api/orders', {picks: getPicks(), source});
    const d = await r.json();
    if (!r.ok) { onError(d.error || 'Could not start the payment.'); throw new Error(d.error); }
    return d;
  }
  async function capture(id, onDone, onError) {
    let d = null;
    try {
      const r = await post('/api/orders/' + id + '/capture');
      d = await r.json().catch(() => null);
      if (r.ok && d && d.status === 'COMPLETED') { onDone(d); return; }
      if (r.ok && d && d.status === 'PENDING') {
        onError('PayPal is still confirming this payment. It counts toward the bill once PayPal completes it.'); return;
      }
      if (d && d.status && d.status !== 'COMPLETED' && r.ok) { onError('PayPal reported ' + d.status + '. You were not charged.'); return; }
    } catch (e) { d = null; }
    // The capture call did not confirm. A webhook may have captured the payment already, so check before saying anything.
    const s = await fetch('/api/orders/' + id + '/status').then((r) => r.json()).catch(() => null);
    if (s && s.status === 'COMPLETED') { onDone(s); return; }
    onError((d && d.error) || 'We could not confirm this payment yet. Check My giving in a minute before trying again.');
  }
  async function mount({mode, clientId, currency, box, fakeButton, getPicks, source, onDone, onError}) {
    box.innerHTML = '';
    if (mode === 'sandbox' && clientId) {
      const pp = await loadSdk(clientId, currency);
      pp.Buttons({style: {layout: 'vertical', shape: 'pill', label: 'pay', height: 48},
        createOrder: async () => (await create(getPicks, source, onError)).id,
        onApprove: async (data) => capture(data.orderID, onDone, onError),
        onCancel: () => onError('Payment cancelled. Nothing was charged.'),
        onError: () => onError('PayPal reported a problem. If a payment went through it appears on My giving within a minute.')}).render(box);
    } else if (fakeButton) {
      fakeButton.onclick = async () => {
        let order; try { order = await create(getPicks, source, onError); } catch (e) { return; }
        const w = window.open(order.approve_url + '&popup=1', 'pp', 'width=440,height=640');
        const onMsg = (ev) => {
          if (!ev.data || !ev.data.fakepaypal || ev.data.orderID !== order.id) return;
          window.removeEventListener('message', onMsg);
          if (ev.data.approved) capture(order.id, onDone, onError); else onError('Payment cancelled. Nothing was charged.');
        };
        window.addEventListener('message', onMsg);
        if (!w) location.href = order.approve_url;
      };
    }
  }
  return {mount, post};
})();
