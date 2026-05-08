
document.addEventListener("DOMContentLoaded", () => {
  const fillMarks = document.querySelectorAll("[data-fill]");
  fillMarks.forEach((el, i) => {
    setTimeout(() => el.classList.add("filled"), 80 * i);
  });

  const defaults = {
    register: {
      fullName: "John Buyer",
      email: "johnbuyer@example.com",
      password: "Buyer12345",
      role: "Buyer"
    },
    transaction: {
      itemDescription: "iPhone 11, good condition",
      transactionAmount: "8500",
      shippingFee: "300",
      sellerEmail: "sellerdemo@example.com",
      courier: "LBC"
    }
  };

  function readJSON(key, fallback) {
    try {
      const raw = sessionStorage.getItem(key);
      return raw ? { ...fallback, ...JSON.parse(raw) } : { ...fallback };
    } catch {
      return { ...fallback };
    }
  }

  function writeJSON(key, value) {
    sessionStorage.setItem(key, JSON.stringify(value));
  }

  function getRegisterMode() {
    return sessionStorage.getItem("formsDemoRegisterMode") || "sample";
  }

  function getRegisterData() {
    return readJSON("formsDemoRegisterData", defaults.register);
  }

  function getTransactionData() {
    return readJSON("formsDemoTransactionData", defaults.transaction);
  }

  async function getLiveSession() {
    try {
      const response = await fetch('/forms-api/session', { headers: { 'Accept': 'application/json' } });
      if (!response.ok) return { logged_in: false, email: '', role: '' };
      return await response.json();
    } catch {
      return { logged_in: false, email: '', role: '' };
    }
  }

  function peso(value) {
    const num = Number(value || 0);
    return new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP", minimumFractionDigits: 2 }).format(num);
  }

  function peso0(value) {
    const num = Number(value || 0);
    return new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP", maximumFractionDigits: 0 }).format(num);
  }

  function buildTransactionId(registerData, transactionData) {
    const initials = (registerData.fullName || "MP")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map(part => part[0].toUpperCase())
      .join("") || "MP";
    const amount = String(Math.round(Number(transactionData.transactionAmount || 0))).padStart(5, "0").slice(-5);
    return `MP-${initials}-${amount}`;
  }

  function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function getNum(input) {
    const cleaned = String(input || "").replace(/[^\d.]/g, "");
    return Number(cleaned || 0);
  }

  const page = location.pathname.split("/").pop();
  const registerMode = getRegisterMode();
  const registerData = getRegisterData();
  const gatedPages = ["create_transaction.html", "seller_joins.html", "buyer_pays.html", "delivery_confirmed.html", "payment_released.html", "finish.html"];

  if (registerMode === "practice" && gatedPages.includes(page)) {
    getLiveSession().then((liveSession) => {
      if (!liveSession.logged_in || liveSession.role !== "buyer") {
        sessionStorage.setItem("formsDemoLoginNotice", "Please log in with your registered Buyer account to continue this sequence.");
        window.location.href = "/forms/login.html";
      }
    });
  }

  if (page === "login.html") {
    setValue("login-email", registerData.email);
    setValue("login-password", registerData.password);
    setText("login-hint", registerMode === "practice"
      ? "These are the exact values entered on the Register screen."
      : "The customer sees exactly which credentials are used on this page.");
  }

  if (page === "create_transaction.html") {
    const modeLabel = document.getElementById("create-mode-label");
    const hint = document.getElementById("create-hint");
    const next = document.getElementById("create-next");
    const fields = {
      itemDescription: document.getElementById("tx-item-description"),
      transactionAmount: document.getElementById("tx-transaction-amount"),
      shippingFee: document.getElementById("tx-shipping-fee"),
      sellerEmail: document.getElementById("tx-seller-email"),
      courier: document.getElementById("tx-courier")
    };
    const errors = {
      itemDescription: document.getElementById("tx-item-description-error"),
      transactionAmount: document.getElementById("tx-transaction-amount-error"),
      shippingFee: document.getElementById("tx-shipping-fee-error"),
      sellerEmail: document.getElementById("tx-seller-email-error"),
      courier: document.getElementById("tx-courier-error")
    };

    const saved = getTransactionData();
    const source = registerMode === "practice" ? saved : defaults.transaction;
    Object.keys(fields).forEach((key) => {
      fields[key].value = source[key] || "";
      fields[key].readOnly = registerMode !== "practice";
      fields[key].placeholder = registerMode === "practice"
        ? {
            itemDescription: "e.g. Wooden dining table, used",
            transactionAmount: "e.g. 10000",
            shippingFee: "e.g. 250",
            sellerEmail: "e.g. seller@example.com",
            courier: "e.g. J&T Express"
          }[key]
        : "";
    });

    if (modeLabel) {
      modeLabel.textContent = registerMode === "practice"
        ? "Complete this screen with your own transaction values."
        : "Sample mode: values are pre-filled for demonstration.";
    }
    if (hint) {
      hint.textContent = registerMode === "practice"
        ? "Enter an item, amounts, seller email, and courier. You will see these same values on the next screens."
        : "The customer sees a complete example transaction before the seller joins.";
    }

    function clearCreateErrors() {
      Object.values(fields).forEach((field) => field.classList.remove("field-invalid"));
      Object.values(errors).forEach((el) => { if (el) el.textContent = ""; });
    }

    function setCreateError(key, message) {
      fields[key].classList.add("field-invalid");
      if (errors[key]) errors[key].textContent = message;
    }

    function validateCreate() {
      clearCreateErrors();
      let valid = true;
      const item = fields.itemDescription.value.trim();
      const amount = getNum(fields.transactionAmount.value);
      const shipping = getNum(fields.shippingFee.value);
      const sellerEmail = fields.sellerEmail.value.trim();
      const courier = fields.courier.value.trim();
      const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

      if (item.length < 3) {
        valid = false;
        setCreateError("itemDescription", "Item description must be at least 3 characters.");
      }
      if (!(amount > 0)) {
        valid = false;
        setCreateError("transactionAmount", "Enter a transaction amount greater than 0.");
      }
      if (shipping < 0) {
        valid = false;
        setCreateError("shippingFee", "Shipping fee cannot be negative.");
      }
      if (!emailPattern.test(sellerEmail)) {
        valid = false;
        setCreateError("sellerEmail", "Enter a valid seller email address.");
      }
      if (courier.length < 2) {
        valid = false;
        setCreateError("courier", "Enter the courier name.");
      }
      if (!valid && hint) {
        hint.textContent = "Please correct the highlighted items before continuing.";
      }
      return valid;
    }

    Object.entries(fields).forEach(([key, field]) => {
      field.addEventListener("input", () => {
        field.classList.remove("field-invalid");
        if (errors[key]) errors[key].textContent = "";
      });
    });

    next?.addEventListener("click", (event) => {
      const data = {
        itemDescription: fields.itemDescription.value.trim(),
        transactionAmount: String(getNum(fields.transactionAmount.value)),
        shippingFee: String(getNum(fields.shippingFee.value)),
        sellerEmail: fields.sellerEmail.value.trim(),
        courier: fields.courier.value.trim()
      };

      if (registerMode === "practice") {
        if (!validateCreate()) {
          event.preventDefault();
          return;
        }
        writeJSON("formsDemoTransactionData", data);
      } else {
        writeJSON("formsDemoTransactionData", defaults.transaction);
      }
    });
  }

  const tx = getTransactionData();
  const itemAmount = getNum(tx.transactionAmount);
  const shippingAmount = getNum(tx.shippingFee);
  const transactionTotal = itemAmount + shippingAmount;
  const totalPlatformFee = Math.max(transactionTotal * 0.03, 100);
  const buyerFee = totalPlatformFee / 2;
  const totalBuyerPays = transactionTotal + buyerFee;
  const sellerFee = totalPlatformFee / 2;
  const sellerReceives = transactionTotal - sellerFee;
  const transactionId = buildTransactionId(registerData, tx);

  if (page === "seller_joins.html") {
    setText("seller-invite-email", tx.sellerEmail || defaults.transaction.sellerEmail);
    setText("seller-transaction-id", transactionId);
  }

  if (page === "buyer_pays.html") {
    setValue("pay-item-amount", peso0(itemAmount));
    setValue("pay-shipping", peso0(shippingAmount));
    setValue("pay-buyer-fee", peso0(buyerFee));
    setValue("pay-total", peso0(totalBuyerPays));
  }

  if (page === "delivery_confirmed.html") {
    setText("delivery-item", tx.itemDescription || defaults.transaction.itemDescription);
    setText("delivery-courier", tx.courier || defaults.transaction.courier);
  }

  if (page === "payment_released.html") {
    setText("release-transaction-amount", peso0(itemAmount));
    setText("release-seller-fee", peso0(sellerFee));
    setText("release-seller-receives", peso0(sellerReceives));
  }

  if (page === "finish.html") {
    setText("finish-summary", `Transaction ${transactionId} is complete. Seller email used: ${tx.sellerEmail || defaults.transaction.sellerEmail}.`);
  }
});
