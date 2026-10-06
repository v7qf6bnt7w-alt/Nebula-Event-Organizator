document.addEventListener("DOMContentLoaded", () => {
  const result = document.querySelector("#scan-result");
  let scanner;

  const checkIn = async (token) => {
    result.className = "scan-message";
    result.textContent = "Validando entrada…";
    try {
      const response = await fetch("/api/check-in/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": document.cookie.split("; ").find((item) => item.startsWith("csrftoken="))?.split("=")[1] || "",
        },
        body: JSON.stringify({ token }),
      });
      const data = await response.json();
      result.classList.add(response.ok ? "success" : "failure");
      result.textContent = response.ok
        ? `ENTRADA VÁLIDA · ${data.buyer} · ${data.event}`
        : data.error === "already_used" ? "ENTRADA YA UTILIZADA" : "ENTRADA NO VÁLIDA";
    } catch {
      result.classList.add("failure");
      result.textContent = "No se pudo validar la entrada. Revisá tu conexión.";
    }
  };

  document.querySelector("#check-manual").addEventListener("click", () => {
    const input = document.querySelector("#manual-token");
    if (input.value.trim()) checkIn(input.value.trim());
  });
  document.querySelector("#start-scanner").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      if (!window.Html5Qrcode) throw new Error("La biblioteca de escaneo no se pudo cargar.");
      scanner = new Html5Qrcode("reader");
      await scanner.start(
        { facingMode: "environment" },
        { fps: 10, qrbox: { width: 240, height: 240 } },
        (decodedText) => {
          scanner.stop().catch(() => {});
          checkIn(decodedText);
        },
      );
    } catch (error) {
      event.currentTarget.disabled = false;
      result.className = "scan-message failure";
      result.textContent = error.message || "No se pudo iniciar la cámara.";
    }
  });
});
