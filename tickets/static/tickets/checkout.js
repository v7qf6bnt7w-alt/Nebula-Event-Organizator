document.addEventListener("DOMContentLoaded", () => {
  const tabs = [...document.querySelectorAll(".ticket-tab")];
  const options = [...document.querySelectorAll(".ticket-option")];
  const total = document.querySelector("#checkout-total");
  const form = document.querySelector("#checkout-form");
  const email = form.querySelector("[name='buyer_email']");
  const emailConfirmation = form.querySelector("[name='buyer_email_confirmation']");
  const confirmationHint = document.createElement("small");
  confirmationHint.className = "field-hint";
  confirmationHint.setAttribute("aria-live", "polite");
  emailConfirmation.after(confirmationHint);
  const money = (amount) => new Intl.NumberFormat("es-AR", {
    style: "currency", currency: "ARS", maximumFractionDigits: 2,
  }).format(amount);

  const updateTotal = () => {
    let amount = 0;
    form.querySelectorAll("[data-quantity]").forEach((input) => {
      const ticketTab = document.querySelector(`[data-tab="${input.dataset.quantity}"]`);
      amount += Number(input.value || 0) * Number(ticketTab?.dataset.price || 0);
    });
    form.querySelectorAll('input[name="seats"]:checked').forEach((seat) => {
      amount += Number(seat.dataset.price);
    });
    total.textContent = money(amount);
  };

  tabs.forEach((tab) => tab.addEventListener("click", () => {
    tabs.forEach((item) => {
      item.classList.toggle("is-active", item === tab);
      item.setAttribute("aria-selected", item === tab ? "true" : "false");
    });
    options.forEach((item) => item.classList.toggle("is-hidden", item.dataset.ticket !== tab.dataset.tab));
  }));
  form.querySelectorAll("[data-step]").forEach((button) => button.addEventListener("click", () => {
    const input = button.parentElement.querySelector("input");
    input.value = Math.max(0, Math.min(10, Number(input.value) + Number(button.dataset.step)));
    updateTotal();
  }));
  form.querySelectorAll("[data-quantity], input[name='seats']").forEach((input) => {
    input.addEventListener("change", updateTotal);
  });
  const checkEmail = () => {
    const mismatch = emailConfirmation.value && email.value.toLowerCase() !== emailConfirmation.value.toLowerCase();
    emailConfirmation.setCustomValidity(mismatch ? "Los correos electrónicos no coinciden." : "");
    confirmationHint.textContent = mismatch ? "Los correos no coinciden todavía." : "";
    confirmationHint.classList.toggle("field-hint-error", Boolean(mismatch));
  };
  email.addEventListener("input", checkEmail);
  emailConfirmation.addEventListener("input", checkEmail);
  form.addEventListener("submit", (event) => {
    const quantity = [...form.querySelectorAll("[data-quantity]")].reduce((sum, item) => sum + Number(item.value || 0), 0);
    const seats = form.querySelectorAll('input[name="seats"]:checked').length;
    if (!quantity && !seats) {
      event.preventDefault();
      window.alert("Elegí al menos una entrada o un lugar.");
    }
  });
  updateTotal();
});
