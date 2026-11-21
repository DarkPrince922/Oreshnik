/** Ready-made MCP servers for the «Мастерская» catalog. Each is an npm package run with `npx -y` in the agent container (it has node);
 * the package names and the variables they read were taken from each package's own README. The panel's check step starts the server
 * in the user's agent container before it is saved as working. */
export interface McpPreset {
  id: string;
  title: string;
  hint: string;
  /** What to do before connecting (where to get a key…), shown on the data step. */
  guide: string;
  transport: "command" | "url";
  name: string;
  description: string;
  command?: string;
  /** Variables the user has to fill in (the value is a secret). */
  env?: { name: string; label: string; placeholder?: string }[];
  urlPlaceholder?: string;
}

export const MCP_PRESETS: McpPreset[] = [
  {
    id: "fetch", title: "Fetch", hint: "скачать страницу как текст или Markdown", transport: "command", name: "fetch",
    description: "Загрузка веб-страниц в HTML, Markdown, тексте или JSON", command: "npx -y mcp-fetch-server",
    guide: "Ключ не нужен. Агент сможет скачать любую страницу и прочитать её как текст.",
  },
  {
    id: "shodan", title: "Shodan", hint: "поиск хостов, портов и сервисов", transport: "command", name: "shodan",
    description: "Запросы к Shodan: хосты, порты, баннеры, уязвимости", command: "npx -y @burtthecoder/mcp-shodan",
    env: [{ name: "SHODAN_API_KEY", label: "Ключ Shodan API" }],
    guide: "Ключ — в аккаунте Shodan: account.shodan.io, раздел «API Key».",
  },
  {
    id: "virustotal", title: "VirusTotal", hint: "репутация файлов, адресов и доменов", transport: "command", name: "virustotal",
    description: "Проверка файлов, URL, IP и доменов в VirusTotal", command: "npx -y @burtthecoder/mcp-virustotal",
    env: [{ name: "VIRUSTOTAL_API_KEY", label: "Ключ VirusTotal API" }],
    guide: "Ключ — в профиле VirusTotal: «API key». Бесплатный ключ ограничен по числу запросов в минуту.",
  },
  {
    id: "github", title: "GitHub", hint: "репозитории, код, issues", transport: "command", name: "github",
    description: "Работа с GitHub: поиск кода, файлы, issues, pull requests", command: "npx -y @modelcontextprotocol/server-github",
    env: [{ name: "GITHUB_PERSONAL_ACCESS_TOKEN", label: "Токен GitHub", placeholder: "github_pat_…" }],
    guide: "Токен — GitHub → Settings → Developer settings → Personal access tokens. Дайте ему только нужные права.",
  },
  {
    id: "tavily", title: "Tavily", hint: "веб-поиск для агента", transport: "command", name: "tavily",
    description: "Поиск в интернете и извлечение страниц через Tavily", command: "npx -y tavily-mcp",
    env: [{ name: "TAVILY_API_KEY", label: "Ключ Tavily", placeholder: "tvly-…" }],
    guide: "Ключ — app.tavily.com. Полезно, если веб-поиск самой модели через ваш шлюз недоступен.",
  },
  {
    id: "brave", title: "Brave Search", hint: "веб-поиск через Brave", transport: "command", name: "brave",
    description: "Поиск в интернете через Brave Search", command: "npx -y @brave/brave-search-mcp-server",
    env: [{ name: "BRAVE_API_KEY", label: "Ключ Brave Search API" }],
    guide: "Ключ — api-dashboard.search.brave.com.",
  },
  {
    id: "memory", title: "Память", hint: "заметки агента между разговорами", transport: "command", name: "memory",
    description: "Граф знаний: агент записывает и вспоминает факты", command: "npx -y @modelcontextprotocol/server-memory",
    guide: "Ключ не нужен. Агент сохраняет факты (стенд, учётки, договорённости) и находит их в следующих разговорах этой сессии.",
  },
  {
    id: "context7", title: "Context7", hint: "свежая документация библиотек", transport: "command", name: "context7",
    description: "Актуальная документация и примеры кода библиотек", command: "npx -y @upstash/context7-mcp",
    guide: "Ключ не обязателен. Агент подтягивает документацию нужной версии библиотеки, а не пишет по памяти.",
  },
  {
    id: "url", title: "Сервер по адресу", hint: "Burp, Caido или свой — уже запущен", transport: "url", name: "",
    description: "", urlPlaceholder: "http://192.168.1.10:9876/mcp",
    guide: "Codex подключается по протоколу streamable HTTP. Сервер должен быть доступен из контейнера агента: укажите адрес компьютера в сети, а не localhost. Если инструмент даёт только SSE-адрес (обычно оканчивается на /sse), нужен переходник.",
  },
  {
    id: "custom", title: "Своя команда", hint: "любой MCP-сервер из npm или pip", transport: "command", name: "",
    description: "", command: "",
    guide: "Команда выполняется в контейнере агента: есть node/npx, python3 и pip. Например: npx -y <пакет>. Ключи задайте переменными ниже.",
  },
];
