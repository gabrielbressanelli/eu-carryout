(() => {
  window.lucide?.createIcons();
  const currentSection = document.querySelector('.setup-nav [aria-current="page"]');
  if (currentSection && currentSection.parentElement.scrollWidth > currentSection.parentElement.clientWidth) {
    currentSection.parentElement.scrollLeft = currentSection.offsetLeft - currentSection.parentElement.offsetLeft - 12;
  }
  document.querySelectorAll("[data-hours-row]").forEach((row) => {
    const checkbox = row.querySelector("[data-hours-closed]");
    const sync = () => {
      row.classList.toggle("is-closed", checkbox.checked);
      row.querySelectorAll('input[type="time"]').forEach((input) => { input.disabled = checkbox.checked; });
    };
    checkbox.addEventListener("change", sync);
    sync();
  });
  document.querySelectorAll("[data-service-row]").forEach((row) => {
    const checkbox = row.querySelector("[data-service-enabled]");
    const sync = () => row.classList.toggle("is-disabled", !checkbox.checked);
    checkbox.addEventListener("change", sync);
    sync();
  });
  document.querySelectorAll("[data-list-search]").forEach((input) => {
    const list = document.querySelector("[data-search-list]");
    if (!list) return;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      let matches = 0;
      list.querySelectorAll("[data-search-row]").forEach((row) => {
        row.hidden = !row.textContent.toLowerCase().includes(query);
        if (!row.hidden) matches += 1;
      });
      list.querySelector("[data-search-empty]").hidden = matches > 0 || !query;
    });
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  document.querySelectorAll("[data-upload-zone]").forEach((zone) => {
    const input = zone.querySelector("[data-image-input]");
    const preview = zone.querySelector("[data-upload-preview]");
    const label = zone.querySelector("[data-upload-name]");
    const error = zone.parentElement.querySelector("[data-upload-error]");
    const original = preview.getAttribute("src");
    const removeButton = zone.parentElement.querySelector("[data-remove-image]");
    const removeValue = zone.closest("form").querySelector("[data-remove-image-value]");
    let removed = removeValue?.value === "True";
    removeButton.hidden = !original || removed;
    if (removed) preview.hidden = true;
    let objectUrl;
    const show = () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      const file = input.files[0];
      error.textContent = "";
      if (!file) return;
      if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
        error.textContent = "Choose a JPG, PNG, or WebP image smaller than 5 MB.";
        input.value = "";
        preview.hidden = !original || removed;
        if (original && !removed) preview.src = original;
        label.textContent = "Drop image here or choose file";
        return;
      }
      objectUrl = URL.createObjectURL(file);
      if (removeValue) removeValue.value = "False";
      removed = false;
      removeButton.hidden = false;
      preview.src = objectUrl;
      preview.hidden = false;
      label.textContent = file.name;
    };
    input.addEventListener("change", show);
    removeButton.addEventListener("click", () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      input.value = "";
      preview.hidden = true;
      preview.removeAttribute("src");
      removed = true;
      if (removeValue) removeValue.value = "True";
      removeButton.hidden = true;
      label.textContent = "Drop image here or choose file";
      error.textContent = "";
    });
    zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("is-dragging"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      if (event.dataTransfer.files.length !== 1) {
        error.textContent = "Choose one image at a time.";
        return;
      }
      input.files = event.dataTransfer.files;
      show();
    });
    window.addEventListener("pagehide", () => { if (objectUrl) URL.revokeObjectURL(objectUrl); });
  });
})();
