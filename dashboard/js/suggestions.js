(() => {
const DEFAULT_WS_BASE_URL = typeof location !== "undefined" ? `ws://${location.host}` : "ws://localhost:8760";
const getSuggestionsWsUrl = () => `${window.WS_BASE_URL || DEFAULT_WS_BASE_URL}/ws/suggestions`;
const suggestionsPanel = document.getElementById("suggestions-panel");

const renderEmptyState = () => {
  if (!suggestionsPanel) {
    return;
  }
  suggestionsPanel.innerHTML = "";
  const empty = document.createElement("div");
  empty.className = "suggestions-empty";
  empty.textContent = window.t("coaching.suggestion_empty_state");
  suggestionsPanel.appendChild(empty);
};

const copyQuestion = async (question, button) => {
  if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
    return;
  }
  try {
    await navigator.clipboard.writeText(question);
    button.classList.add("is-copied");
    button.dataset.copiedLabel = button.textContent;
    button.textContent = window.t("suggestions.copied_label");
    setTimeout(() => {
      button.classList.remove("is-copied");
      if (button.dataset.copiedLabel) {
        button.textContent = button.dataset.copiedLabel;
      }
    }, 1200);
  } catch (error) {
    console.error("Kopieren van suggestie mislukt", error);
  }
};

const buildSuggestionItem = (question) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "suggestion-item";
  button.textContent = question;
  button.addEventListener("click", () => {
    copyQuestion(question, button);
  });
  return button;
};

const renderQuestionList = (questions, { streaming }) => {
  if (!suggestionsPanel) {
    return;
  }
  suggestionsPanel.innerHTML = "";
  questions.forEach((question) => {
    const item = buildSuggestionItem(question);
    if (streaming) {
      item.classList.add("suggestion-item--streaming");
    }
    suggestionsPanel.appendChild(item);
  });
};

const renderSuggestions = (payload) => {
  if (!suggestionsPanel || !payload || payload.type !== "suggestion" || !Array.isArray(payload.questions)) {
    return;
  }

  const questions = payload.questions.filter((question) => typeof question === "string" && question.trim());
  if (questions.length === 0) {
    renderEmptyState();
    return;
  }

  renderQuestionList(questions, { streaming: false });
};

const renderSuggestionsPartial = (payload) => {
  if (
    !suggestionsPanel ||
    !payload ||
    payload.type !== "suggestion_partial" ||
    !Array.isArray(payload.questions)
  ) {
    return;
  }

  const questions = payload.questions.filter((question) => typeof question === "string" && question.trim());
  if (questions.length === 0) {
    return;
  }

  // Same panel the final `suggestion` event renders into: growing text is visible
  // in place, then the final event replaces the streaming markers with plain items.
  renderQuestionList(questions, { streaming: true });
};

window.resetSuggestionsPanel = renderEmptyState;

if (suggestionsPanel) {
  let socket;
  let retryCount = 0;

  const connect = () => {
    socket = new WebSocket(getSuggestionsWsUrl());

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "suggestion_partial") {
          renderSuggestionsPartial(payload);
        } else {
          renderSuggestions(payload);
        }
      } catch (error) {
        console.error("Invalid suggestion message", error);
      }
    });

    socket.addEventListener("close", () => {
      const delay = Math.min(1000 * 2 ** retryCount, 30000);
      retryCount += 1;
      setTimeout(connect, delay);
    });

    socket.addEventListener("open", () => {
      retryCount = 0;
    });

    socket.addEventListener("error", () => {
      socket.close();
    });
  };

  window.SalesCopilotI18n.ready.then(renderEmptyState);
  connect();
}
})();
