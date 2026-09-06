<p align="center">
  <img src="docs/images/breaktwenty-readme-header-dark.png" alt="BreakTwenty" width="300">
</p>

<p align="center">
  See clearly. Move deliberately!
</p>

<p align="center">
  <strong> Linux · Windows · macOS</strong>
</p>

---

![BreakTwenty Dashboard](docs/images/Dashboard.png)

## What is BreakTwenty?

Remember when breaking a twenty-dollar bill at the store meant turning it into smaller, more useful money, enough for the decent shopping and the leftover change in your pocket?

BreakTwenty works the same way with your financial life: it consolidates your assets/liabilities in one place, then breaks everything down into clear, useful pieces so you can see how your everyday choices move you closer to the life you’re building!

BreakTwenty currently supports most major Canadian financial institutions.

Your BreakTwenty data is stored locally and encrypted.

## ✨ Features

* **Net Worth Dashboard** — track assets, liabilities, account balances, allocation, and historical net worth.
* **Accounts** — view connected banks, brokerages, crypto accounts, cash, manual institutions, property, vehicles, private investments, and debt.
* **Transactions** — search, filter, categorize, edit, and review transaction history.
* **Cash Flow** — understand income, spending, category breakdowns, recurring expenses, and money movement.
* **Investments** — track holdings, allocation, performance, options, crypto, dividend income, and interest income.
* **Financial Institution Connections** — connect supported banks, brokerages, exchanges, and financial services.
* **Manual Tracking** — add accounts and assets that cannot be connected automatically.
* **Import & Export** — import supported transaction and balance history and export your financial data to CSV.
* **Local Data Storage** — financial data and saved authentication material remain on the local BreakTwenty installation.
* **Encrypted Database** — local application data is protected with SQLCipher encryption.

## 📸 App examples

### Accounts

![BreakTwenty Accounts](docs/images/Accounts.png)

### Investments

![BreakTwenty Investments](docs/images/Investments.png)

## 📦 Download

For normal use, download the latest BreakTwenty release from:

**[BreakTwenty Releases](https://github.com/InvictusVIII/BreakTwenty/releases/latest)**

Official releases are built for:

* **Linux x64** — AppImage and `.deb`
* **Windows x64** — installer
* **macOS Apple Silicon** — `.dmg`

Official Windows and macOS releases are signed through the BreakTwenty release pipeline.

Intel macOS users can build BreakTwenty locally from source using the instructions below.

## 🛠️ Build from Source

BreakTwenty packages are built natively for the target operating system.

### Requirements

All platforms require:

* Git
* Node.js **22.12 or newer**
* npm
* `tar`

Clone the repository:

```bash
git clone https://github.com/InvictusVIII/BreakTwenty.git
cd BreakTwenty
npm --prefix frontend ci
npm --prefix desktop ci
```

### Linux x64

Build the AppImage and Debian package:

```bash
npm --prefix desktop run dist:linux
```

The completed packages are written under:

```text
desktop/dist/electron/
```

### Windows x64

Open PowerShell or another supported terminal in the cloned repository and run:

```powershell
npm --prefix desktop run dist:win
```

The completed Windows installer is written under:

```text
desktop/dist/electron/
```

### macOS — Apple Silicon

Install the Xcode Command Line Tools if they are not already installed:

```bash
xcode-select --install
```

Then build:

```bash
npm --prefix desktop run dist:mac -- --arm64
```

The completed macOS package is written under:

```text
desktop/dist/electron/
```

### macOS — Intel

Intel macOS builds additionally require **Rust 1.83 or newer** because one packaged dependency must be compiled locally.

Install the Xcode Command Line Tools if necessary:

```bash
xcode-select --install
```

Then build:

```bash
npm --prefix desktop run dist:mac -- --x64
```

The completed macOS package is written under:

```text
desktop/dist/electron/
```

## 🤝 Contributing

Bug reports, improvements, feature suggestions, documentation changes, and pull requests are welcome.

Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** before submitting a contribution.

## 🔒 Security

Found a suspected security issue? Please **do not post vulnerability details publicly**.

Follow the private reporting instructions in **[SECURITY.md](.github/SECURITY.md)**.

## ⚖️ License

## Source Available — Not Open Source

BreakTwenty is **source-available, not open source**.

Personal and non-commercial use, modification, and permitted redistribution are governed by the **[BreakTwenty Source-Available License 1.0](LICENSE.md)**.

Commercial use, organizational use, business use, revenue-generating use, and monetization require separate written permission.

The exact terms in [LICENSE.md](LICENSE.md) control.
