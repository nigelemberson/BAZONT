const steps = [
  {
    title: "Buyer creates transaction",
    subtitle: "The buyer enters brief details and invites the seller to join the Platform.",
    caption: "Buyer creates transaction and contacts the seller by email, messenger, whatsapp etc.",
    status: "",
    setup() {
      show("cardCreate", "ringBuyer", "arrowLeft");
    }
  },
  {
    title: "Seller joins transaction",
    subtitle: "The seller receives the link and joins the transaction.",
    caption: "Seller joins transaction from the invite link.",
    status: "",
    setup() {
      if (pendingCreateToInviteTravel) {
        pendingCreateToInviteTravel = false;
        animateCreateToInviteOnStep2();
      } else {
        show("cardInvite", "ringSeller", "arrowRight");
      }
    }
  },
  {
    title: "Buyer makes payment",
    subtitle: "The platform protects the funds while delivery has not yet been confirmed.",
    caption: "Buyer makes payment. Funds are held securely until delivery is confirmed.",
    status: "",
    setup() {
      show("cardHold", "ringPlatform", "laneMoney", "moneyChip");
      const chip = document.getElementById("moneyChip");
      chip.classList.add("move-left-to-vault");
    }
  },
  {
    title: "Seller ships item",
    subtitle: "The item moves while the buyer’s funds remain protected in the platform's account.",
    caption: "Seller ships item. Funds remain protected until delivery is confirmed.",
    status: "",
    setup() {
      show("cardShip", "laneParcel", "parcelChip");
      const chip = document.getElementById("parcelChip");
      chip.style.left = "calc(100% - 170px)";
      chip.classList.add("move-right-to-left-safe");
    }
  },
  {
    title: "Courier confirms delivery",
    subtitle: "Delivery is confirmed by the courier, (or by the buyer inside the platform).",
    caption: "Courier confirms delivery.",
    status: "",
    setup() {
      show("cardConfirm", "ringBuyer");
    }
  },
  {
    title: "Platform releases payment",
    subtitle: "After the courier confirms delivery, the platform releases the payment to the seller.",
    caption: "Platform releases payment to the seller.",
    status: "",
    setup() {
      show("cardRelease", "ringSeller", "laneMoney", "moneyChip");
      const chip = document.getElementById("moneyChip");
      chip.style.left = "50%";
      chip.style.transform = "translateX(-50%)";
      chip.classList.add("move-center-to-right-safe");
    }
  }
];

let currentStep = getRequestedStepIndex();
let pendingCreateToInviteTravel = false;
let autoPlay = false;
let timer = null;
let transitionTimers = [];
const cardCreateOriginalHtml = document.getElementById("cardCreate").innerHTML;
const cardInviteOriginalHtml = document.getElementById("cardInvite").innerHTML;

const hero = document.getElementById("hero");
const demo = document.getElementById("demo");
const startBtn = document.getElementById("startBtn");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");
const restartBtn = document.getElementById("restartBtn");
const autoBtn = document.getElementById("autoBtn");
const progressFill = document.getElementById("progressFill");
const stage = document.getElementById("stage");
const sceneTitle = document.getElementById("sceneTitle");
const sceneSubtitle = document.getElementById("sceneSubtitle");
const captionBox = document.getElementById("captionBox");
const demoStatus = document.getElementById("demoStatus");
const stepItems = Array.from(document.querySelectorAll(".step-item"));

const animationPageIds = ["4", "5", "6", "7", "8", "9"];
const pageIdBadge = document.getElementById("page-id-badge");

const sceneIds = [
  "cardCreate","cardInvite","cardHold","cardShip","cardConfirm","cardRelease",
  "ringBuyer","ringSeller","ringPlatform","laneMoney","laneParcel",
  "moneyChip","parcelChip","arrowLeft","arrowRight"
];


function getRequestedStepIndex() {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get("step");
  const fromQuery = parseInt(raw, 10);
  if (!Number.isNaN(fromQuery) && fromQuery >= 1 && fromQuery <= steps.length) {
    return fromQuery - 1;
  }

  const hashMatch = window.location.hash.match(/step(\d+)/i);
  if (hashMatch) {
    const fromHash = parseInt(hashMatch[1], 10);
    if (!Number.isNaN(fromHash) && fromHash >= 1 && fromHash <= steps.length) {
      return fromHash - 1;
    }
  }

  return 0;
}


startBtn.addEventListener("click", () => {
  hero.classList.add("hidden");
  demo.classList.remove("hidden");
  currentStep = 0;
  currentStep = getRequestedStepIndex();
renderStep();
});

prevBtn.addEventListener("click", () => {
  stopTimer();
  if (currentStep > 0) {
    currentStep -= 1;
    renderStep();
  } else {
    window.location.href = "/intro";
  }
});

nextBtn.addEventListener("click", () => {
  stopTimer();
  if (currentStep < steps.length - 1) {
    pendingCreateToInviteTravel = currentStep === 0;
    currentStep += 1;
    renderStep();
  } else {
    window.location.href = "/register";
  }
});

restartBtn.addEventListener("click", () => {
  stopTimer();
  currentStep = 0;
  renderStep();
});

autoBtn.addEventListener("click", () => {
  autoPlay = !autoPlay;
  autoBtn.textContent = `Auto Play: ${autoPlay ? "On" : "Off"}`;
  if (autoPlay) scheduleAdvance();
  else stopTimer();
});

function renderStep() {
  resetScene();
  document.body.classList.toggle("fit-step-page", currentStep !== 4);
  const step = steps[currentStep];
  stage.classList.add(`step-${currentStep + 1}`);
  sceneTitle.textContent = step.title;
  sceneSubtitle.textContent = step.subtitle;
  captionBox.textContent = step.caption;
  demoStatus.textContent = step.status;
  if (pageIdBadge) pageIdBadge.textContent = animationPageIds[currentStep] || "4";
  progressFill.style.width = `${((currentStep + 1) / steps.length) * 100}%`;

  stepItems.forEach((item, index) => {
    item.classList.toggle("active", index === currentStep);
  });

  const platformIcon = document.getElementById("platformIcon");
  if (platformIcon) platformIcon.textContent = (currentStep >= 2 && currentStep <= 5) ? "🏦" : "🛡️";

  step.setup();
  if (autoPlay) scheduleAdvance();
}

function resetScene() {
  stopTimer();
  clearTransitionTimers();
  stage.classList.remove("step-1", "step-2", "step-3", "step-4", "step-5", "step-6");

  const cardCreate = document.getElementById("cardCreate");
  if (cardCreate) {
    cardCreate.classList.remove("card-travel-create-invite");
    cardCreate.innerHTML = cardCreateOriginalHtml;
    cardCreate.removeAttribute("aria-label");
  }

  const cardInvite = document.getElementById("cardInvite");
  if (cardInvite) cardInvite.innerHTML = cardInviteOriginalHtml;
  sceneIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add("hidden");
  });

  const money = document.getElementById("moneyChip");
  const parcel = document.getElementById("parcelChip");
  const platformIcon = document.getElementById("platformIcon");
  if (platformIcon) platformIcon.textContent = "🛡️";

  money.classList.remove("move-left-to-center", "move-left-to-vault", "move-center-to-right-safe");
  parcel.classList.remove("move-right-to-left-safe");

  money.style.left = "0";
  money.style.top = "0";
  money.style.transform = "";
  parcel.style.left = "0";
}

function show(...ids) {
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("hidden");
  });
}

function animateCreateToInviteOnStep2() {
  const card = document.getElementById("cardCreate");
  const invite = document.getElementById("cardInvite");
  if (!card || !invite) return;

  card.innerHTML = cardCreateOriginalHtml;
  card.removeAttribute("aria-label");
  show("cardCreate", "ringSeller", "arrowRight");
  card.classList.add("card-travel-create-invite");

  transitionTimers.push(window.setTimeout(() => {
    card.innerHTML = cardInviteOriginalHtml;
    card.setAttribute("aria-label", "Receive invitation");
  }, 1500));

  transitionTimers.push(window.setTimeout(() => {
    card.classList.add("hidden");
    card.classList.remove("card-travel-create-invite");
    card.innerHTML = cardCreateOriginalHtml;
    card.removeAttribute("aria-label");
    invite.classList.remove("hidden");
  }, 3100));
}

function clearTransitionTimers() {
  transitionTimers.forEach(id => window.clearTimeout(id));
  transitionTimers = [];
}

function scheduleAdvance() {
  stopTimer();
  timer = window.setTimeout(() => {
    currentStep = (currentStep + 1) % steps.length;
    renderStep();
  }, 6750);
}

function stopTimer() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}

renderStep();
