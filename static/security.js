(() => {
    const token = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
    if (!token) {
        return;
    }

    const ensureFormToken = (form) => {
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        const method = (form.getAttribute("method") || "get").toLowerCase();
        if (method !== "post") {
            return;
        }
        let input = form.querySelector('input[name="csrf_token"]');
        if (!input) {
            input = document.createElement("input");
            input.type = "hidden";
            input.name = "csrf_token";
            form.appendChild(input);
        }
        input.value = token;
    };

    document.querySelectorAll("form").forEach(ensureFormToken);
    document.addEventListener("submit", (event) => ensureFormToken(event.target), true);

    const originalFetch = window.fetch.bind(window);
    window.fetch = (resource, options = {}) => {
        const method = String(options.method || "GET").toUpperCase();
        if (method === "GET" || method === "HEAD" || method === "OPTIONS") {
            return originalFetch(resource, options);
        }
        const headers = new Headers(options.headers || {});
        if (!headers.has("X-CSRF-Token")) {
            headers.set("X-CSRF-Token", token);
        }
        return originalFetch(resource, { ...options, headers });
    };
})();

