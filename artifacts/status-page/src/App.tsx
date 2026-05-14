function App() {
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
        color: "#f1f5f9",
        fontFamily: "Inter, system-ui, sans-serif",
        padding: "2rem",
        textAlign: "center",
      }}
    >
      <div>
        <div style={{ fontSize: "4rem", marginBottom: "1rem" }}>🤖</div>
        <h1 style={{ fontSize: "2rem", margin: 0, fontWeight: 700 }}>
          Ruslan Personal Helper
        </h1>
        <p style={{ marginTop: "1rem", opacity: 0.75, fontSize: "1.05rem" }}>
          Telegram-бот работает 24/7
        </p>
        <p style={{ marginTop: "0.5rem", opacity: 0.5, fontSize: "0.9rem" }}>
          Открой Telegram и напиши боту
        </p>
      </div>
    </div>
  );
}

export default App;
