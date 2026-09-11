(() => {
  const config = document.getElementById("cart-config");
  if (!config) return;
  const modalEl = document.getElementById("itemModal");
  const itemForm = modalEl?.querySelector("[data-item-form]");
  const checkoutForm = document.querySelector("[data-checkout-form]");
  const pickup = document.querySelector("[data-pickup-select]");
  let queue = Promise.resolve();
  let cartValid = true;
  let cartCount = Number(document.querySelector("[data-cart-count]")?.textContent || 0);
  let paying = false;
  let itemData;
  let quoteVersion = 0;
  let loadVersion = 0;
  let quoteAbort;
  let returnToCart = false;
  let itemSaving = false;

  function csrf() {
    return document.querySelector('[name="csrfmiddlewaretoken"]')?.value ||
      document.cookie.split("; ").find(value => value.startsWith("csrftoken="))?.split("=")[1] || "";
  }
  function enqueue(action) {
    const pending = queue.then(action);
    queue = pending.catch(() => {});
    return pending;
  }
  async function request(url, body) {
    const response = await fetch(url, {
      method: body ? "POST" : "GET", credentials: "same-origin",
      headers: {"X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf()},
      ...(body ? {body} : {}),
    });
    const data = await response.json();
    return {response, data};
  }
  function syncPay() {
    const button = document.querySelector("[data-pay-button]");
    if (button) button.disabled = paying || cartCount === 0 || !cartValid || !pickup?.value;
  }
  function replaceCart(data) {
    if (data.cart_html !== undefined) document.getElementById("cartSummaryContainer").innerHTML = data.cart_html;
    const panel = document.getElementById("checkoutSummaryPanel");
    if (panel && data.checkout_html !== undefined) panel.innerHTML = data.checkout_html;
    if (data.cart_qty !== undefined) {
      cartCount = data.cart_qty;
      document.querySelectorAll("[data-cart-count]").forEach(badge => {
        badge.textContent = cartCount;
        badge.classList.toggle("d-none", cartCount === 0);
        badge.setAttribute("aria-label", `Cart items: ${cartCount}`);
      });
    }
    if (data.cart_valid !== undefined) cartValid = data.cart_valid;
    if (data.cart_revision && checkoutForm) checkoutForm.elements.cart_revision.value = data.cart_revision;
    syncPay();
  }
  async function mutate(url, body) {
    const {response, data} = await request(url, body);
    replaceCart(data);
    if (!response.ok || data.ok === false) throw new Error(data.error || "Cart update failed.");
    return data;
  }
  async function refreshCart() {
    const {response, data} = await request(config.dataset.summaryUrl);
    if (response.ok) replaceCart(data);
  }
  function showAvailability(data) {
    if (!pickup) return;
    const selected = pickup.value;
    pickup.replaceChildren();
    if (data.asap_available) pickup.add(new Option("As soon as possible", "asap"));
    data.slots.forEach(slot => pickup.add(new Option(slot.label, slot.value)));
    if (!pickup.options.length) pickup.add(new Option("No pickup times available", ""));
    if ([...pickup.options].some(option => option.value === selected)) pickup.value = selected;
    document.querySelector("[data-availability-message]").textContent = data.message;
    syncPay();
  }
  async function refreshAvailability() {
    if (!config.dataset.availabilityUrl) return;
    const {response, data} = await request(config.dataset.availabilityUrl);
    if (response.ok) showAvailability(data);
  }

  function itemBody() {
    const body = new FormData(itemForm);
    body.set("item_id", itemData.item.id);
    itemForm.querySelectorAll("[data-option-id]:checked").forEach(input => body.append("option_ids", input.value));
    if (itemData.line) {
      body.set("line_key", itemData.line.key);
      body.set("configure", "1");
    }
    return body;
  }
  async function quote() {
    if (!itemData || itemSaving) return;
    const version = ++quoteVersion;
    const button = itemForm.querySelector("[data-item-submit]");
    const error = itemForm.querySelector("[data-item-error]");
    button.disabled = true;
    itemForm.querySelector("[data-item-total]").textContent = "";
    quoteAbort?.abort();
    quoteAbort = new AbortController();
    try {
      const response = await fetch(itemData.quote_url, {
        method: "POST", body: itemBody(), credentials: "same-origin",
        headers: {"X-CSRFToken": csrf()}, signal: quoteAbort.signal,
      });
      const data = await response.json();
      if (version !== quoteVersion) return;
      error.textContent = data.error || "";
      if (response.ok) {
        itemForm.querySelector("[data-item-total]").textContent = `$${data.total}`;
        button.disabled = false;
      }
    } catch (errorValue) {
      if (errorValue.name !== "AbortError" && version === quoteVersion) error.textContent = "Could not update the price. Please try again.";
    }
  }
  function buildGroups(item, line) {
    const container = itemForm.querySelector("[data-modifier-groups]");
    container.replaceChildren();
    const selected = line ? new Set(line.options.map(option => option.id)) : null;
    for (const group of item.groups) {
      const fieldset = document.createElement("fieldset");
      fieldset.className = "modifier-group";
      const legend = document.createElement("legend");
      legend.textContent = group.name;
      fieldset.append(legend);
      const rule = document.createElement("p");
      rule.className = "modifier-rule";
      const minimum = Math.max(group.required ? 1 : 0, group.min_choices);
      rule.textContent = minimum > 0
        ? `Choose at least ${minimum}${group.max_choices ? `, up to ${group.max_choices}` : ""}`
        : (group.max_choices ? `Optional · Choose up to ${group.max_choices}` : "Optional");
      fieldset.append(rule);
      if (group.max_choices === 1 && minimum === 0) {
        const label = document.createElement("label");
        label.className = "modifier-choice";
        const input = document.createElement("input");
        input.type = "radio";
        input.name = `group_${group.id}`;
        input.className = "form-check-input";
        input.checked = !group.options.some(option => selected ? selected.has(option.id) : option.is_default);
        label.append(input, document.createTextNode("None"));
        fieldset.append(label);
      }
      for (const option of group.options) {
        const label = document.createElement("label");
        label.className = "modifier-choice";
        const input = document.createElement("input");
        input.type = group.max_choices === 1 ? "radio" : "checkbox";
        input.name = `group_${group.id}`;
        input.value = option.id;
        input.dataset.optionId = option.id;
        input.className = "form-check-input";
        input.checked = selected ? selected.has(option.id) : option.is_default;
        const name = document.createElement("span");
        name.className = "modifier-name";
        name.textContent = option.name;
        const price = document.createElement("span");
        price.className = "modifier-price";
        const adjustments = [];
        if (Number(option.price_multiplier) !== 1) adjustments.push(`Base × ${option.price_multiplier}`);
        if (Number(option.price_delta)) adjustments.push(`${Number(option.price_delta) < 0 ? "-" : "+"}$${Math.abs(Number(option.price_delta)).toFixed(2)}`);
        price.textContent = adjustments.join(" · ");
        label.append(input, name, price);
        fieldset.append(label);
      }
      container.append(fieldset);
    }
  }
  async function openItem(url) {
    if (itemSaving) return;
    const version = ++loadVersion;
    itemData = null;
    quoteVersion++;
    quoteAbort?.abort();
    itemForm.reset();
    itemForm.querySelector("[data-item-loading]").hidden = false;
    itemForm.querySelector("[data-item-content]").hidden = true;
    itemForm.querySelector("[data-item-error]").textContent = "";
    itemForm.querySelector("[data-item-total]").textContent = "";
    itemForm.querySelector("[data-item-submit]").disabled = true;
    modalEl.querySelector("#itemModalTitle").textContent = "Customize item";
    const show = () => bootstrap.Modal.getOrCreateInstance(modalEl).show();
    const offcanvas = document.getElementById("cartOffcanvas");
    returnToCart = offcanvas.classList.contains("show");
    if (returnToCart) {
      offcanvas.addEventListener("hidden.bs.offcanvas", show, {once:true});
      bootstrap.Offcanvas.getOrCreateInstance(offcanvas).hide();
    } else show();
    try {
      const {response, data} = await request(url);
      if (version !== loadVersion) return;
      if (!response.ok) throw new Error(data.error || "This item is unavailable.");
      itemData = data;
      itemForm.action = data.action_url;
      modalEl.querySelector("#itemModalTitle").textContent = data.item.name;
      itemForm.querySelector("[data-item-description]").textContent = data.item.description;
      const image = itemForm.querySelector("[data-item-image]");
      image.hidden = !data.item.image;
      if (data.item.image) image.src = data.item.image;
      else image.removeAttribute("src");
      image.alt = data.item.name;
      itemForm.elements.quantity.value = data.line?.quantity || 1;
      itemForm.elements.note.value = data.line?.note || "";
      itemForm.querySelector("[data-item-command]").textContent = data.line ? "Save changes" : "Add to cart";
      buildGroups(data.item, data.line);
      itemForm.querySelector("[data-item-content]").hidden = false;
      quote();
    } catch (error) {
      itemForm.querySelector("[data-item-error]").textContent = error.message;
    } finally {
      if (version === loadVersion) itemForm.querySelector("[data-item-loading]").hidden = true;
    }
  }
  modalEl?.addEventListener("hidden.bs.modal", () => {
    loadVersion++;
    quoteVersion++;
    quoteAbort?.abort();
    itemData = null;
    if (returnToCart) bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("cartOffcanvas")).show();
  });
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-customize-url]");
    if (button && !paying) openItem(button.dataset.customizeUrl);
  });
  itemForm?.addEventListener("change", quote);
  itemForm?.elements.quantity.addEventListener("input", quote);
  itemForm?.addEventListener("submit", async event => {
    event.preventDefault();
    if (!itemData || itemSaving) return;
    itemSaving = true;
    const version = loadVersion;
    const button = itemForm.querySelector("[data-item-submit]");
    const body = itemBody();
    const url = itemForm.action;
    button.disabled = true;
    try {
      await enqueue(() => mutate(url, body));
      if (version === loadVersion) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
    } catch (error) {
      itemForm.querySelector("[data-item-error]").textContent = error.message;
      button.disabled = false;
    } finally {
      itemSaving = false;
    }
  });

  document.addEventListener("submit", async event => {
    const form = event.target;
    if (!form.matches("[data-cart-remove-form], [data-cart-update-form]")) return;
    event.preventDefault();
    if (paying) return;
    const body = new FormData(form);
    try { await enqueue(() => mutate(form.action, body)); }
    catch (error) { window.alert(error.message); }
  });
  document.addEventListener("change", async event => {
    const input = event.target;
    if (!input.matches("[data-cart-update-form] input[name='quantity']")) return;
    if (paying) return;
    const form = input.closest("form");
    const body = new FormData(form);
    input.readOnly = true;
    try { await enqueue(() => mutate(form.action, body)); }
    catch (error) { window.alert(error.message); await enqueue(refreshCart); }
    finally { input.readOnly = false; }
  });
  pickup?.addEventListener("change", syncPay);
  checkoutForm?.addEventListener("submit", async event => {
    event.preventDefault();
    if (paying) return;
    paying = true;
    syncPay();
    const errorNode = document.querySelector("[data-checkout-error]");
    errorNode.textContent = "";
    try {
      await queue;
      const {response, data} = await request(checkoutForm.action, new FormData(checkoutForm));
      replaceCart(data);
      if (data.availability) showAvailability(data.availability);
      if (!response.ok || data.error) throw new Error(data.error || "Could not start payment.");
      window.location.assign(data.checkout_url);
    } catch (error) {
      errorNode.textContent = error.message;
      paying = false;
      syncPay();
    }
  });
  enqueue(refreshCart).catch(() => {});
  refreshAvailability().catch(() => {});
  window.addEventListener("pageshow", event => {
    if (event.persisted) { paying = false; enqueue(refreshCart); refreshAvailability(); }
  });
})();
