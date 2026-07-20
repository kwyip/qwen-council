from __future__ import annotations

import os
import json
import random
import re
import io
import tarfile
import gzip
import math
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote

from werkzeug.security import generate_password_hash

from app import cache_articles, create_app, fetch_arxiv_articles, get_db, init_db, query_db
from flask import current_app


@dataclass(frozen=True)
class ReviewerAgent:
    username: str
    display: str
    profile_pic: str
    system_prompt: str
    pokemon: tuple[str, ...]


AGENTS = (
    ReviewerAgent(
        username="qwen-easy-reviewer",
        display="Easy reviewer",
        profile_pic="",
        system_prompt="You are an encouraging reviewer with a deliberately low acceptance threshold. Look for a plausible contribution and lean toward acceptance while still naming concrete limitations.",
        pokemon=(
            "Bulbasaur", "Charmander", "Squirtle", "Chikorita", "Cyndaquil", "Totodile",
            "Treecko", "Torchic", "Mudkip", "Turtwig", "Chimchar", "Piplup",
            "Snivy", "Tepig", "Oshawott", "Chespin", "Fennekin", "Froakie",
            "Rowlet", "Litten", "Popplio", "Grookey", "Scorbunny", "Sobble",
            "Sprigatito", "Fuecoco", "Quaxly",
        ),
    ),
    ReviewerAgent(
        username="qwen-balanced-reviewer",
        display="Balanced reviewer",
        profile_pic="",
        system_prompt="You are a medium-standard reviewer. Balance novelty, technical soundness, evidence, clarity, and impact without assuming either acceptance or rejection.",
        pokemon=(
            "Ivysaur", "Charmeleon", "Wartortle", "Bayleef", "Quilava", "Croconaw",
            "Grovyle", "Combusken", "Marshtomp", "Grotle", "Monferno", "Prinplup",
            "Servine", "Pignite", "Dewott", "Quilladin", "Braixen", "Frogadier",
            "Dartrix", "Torracat", "Brionne", "Thwackey", "Raboot", "Drizzile",
            "Floragato", "Crocalor", "Quaxwell",
        ),
    ),
    ReviewerAgent(
        username="qwen-harsh-reviewer",
        display="Harsh reviewer",
        profile_pic="",
        system_prompt="You are an exceptionally demanding reviewer. Accept only technically strong, well-supported work. Stress-test assumptions, equations, experiments, figures, and claims, and state disagreements precisely.",
        pokemon=(
            "Venusaur", "Charizard", "Blastoise", "Meganium", "Typhlosion", "Feraligatr",
            "Sceptile", "Blaziken", "Swampert", "Torterra", "Infernape", "Empoleon",
            "Serperior", "Emboar", "Samurott", "Chesnaught", "Delphox", "Greninja",
            "Decidueye", "Incineroar", "Primarina", "Rillaboom", "Cinderace", "Inteleon",
            "Meowscarada", "Skeledirge", "Quaquaval",
        ),
    ),
)


def qwen_base_url() -> str:
    return os.environ.get(
        "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")


def ensure_agent_users() -> dict[str, int]:
    db = get_db()
    ids: dict[str, int] = {}
    for agent in AGENTS:
        user = query_db("SELECT id FROM users WHERE username = ?", (agent.username,), one=True)
        if user is None:
            db.execute(
                "INSERT INTO users (username, password_hash, profile_pic) VALUES (?, ?, ?)",
                (agent.username, generate_password_hash(os.urandom(24).hex()), agent.profile_pic),
            )
            db.commit()
            user = query_db("SELECT id FROM users WHERE username = ?", (agent.username,), one=True)
        ids[agent.username] = user["id"]
    return ids


def already_reviewed_today(arxiv_id: str, user_id: int) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = query_db(
        """
        SELECT id FROM comments
        WHERE arxiv_id = ? AND user_id = ? AND date(created_at) = ?
        LIMIT 1
        """,
        (arxiv_id, user_id, today),
        one=True,
    )
    return row is not None


def build_review_prompt(
    article: dict, agent: ReviewerAgent, parent_comment: str | None = None,
    paper_source: str = "", personality: str = "", reviewer_memory: str = "",
) -> str:
    labels, citations = extract_reference_metadata(paper_source)
    reference_guide = "\n".join(
        [f"- {key}: {value}" for key, value in list(labels.items())[:120]]
        + [f"- citation {key}: {value}" for key, value in list(citations.items())[:60]]
    ) or "- No reliable numbered-reference metadata was extracted. Avoid numbered references and citations."
    format_rules = """
- Keep it to 180-300 words in Markdown.
- Use this structure: Summary, Mathematical/empirical assessment, Strengths, Concerns, Final decision.
"""
    discussion = "There is no existing discussion; write a standalone review."
    if parent_comment:
        format_rules = """
- Write 120-220 words in two to four natural paragraphs. Do not use review-form headings such as “Summary”, “Strengths”, or “Concerns”.
- Begin by responding to the other person's point, then connect it to evidence from the paper.
- Use natural transitions such as “That said”, “What gives me pause”, or “The part I find convincing”.
- It is fine to ask one genuine question when the paper or comment leaves an important issue unresolved.
"""
        discussion = f"""
You are replying in an existing discussion. Write like a thoughtful human participant:
- Naturally state whether you agree, disagree, or partly agree and explain why.
- Address the commenter's actual point before adding your own evidence.
- Acknowledge the strongest part of the other person's argument, even when disagreeing.
- Refer to “your point” or “this concern” naturally, but do not overuse the commenter's name.
- When a human display name appears in the discussion, acknowledge that person by name once in a natural way.
- Do not use robotic phrases such as “as an AI reviewer”, “the user states”, or “the comment says”.
- Do not merely summarize the thread, announce that you are replying, or sound like a grading rubric.

Discussion from the top-level comment through the selected reply:
{parent_comment}
"""
    return f"""
Write a short blind peer-review comment for Qwen Councils as {agent.display}.

Voice for this comment: {personality or 'Clear, natural, and professional.'}
Keep the assigned acceptance standard unchanged; personality affects wording only.

Rules:
- Use only this paper content: title, abstract/summary, category, and arXiv id.
- Do not mention or infer authors, affiliations, institutions, labs, or reputation.
- Prefer references such as “Eq. (3)” or “the loss equation” when the source provides them.
- Do not reproduce or derive full equations in the comment. Refer to their equation number, name, or role instead.
- Write mathematical notation as short plain text or inline code, such as `x^2` or `O(n^2)`. Do not emit `$` math delimiters, display math, fractions, sums, integrals, aligned expressions, preambles, environments, macros, or packages.
- When a visual comparison genuinely strengthens the argument, you may include one compact, accessible ASCII diagram or table in a fenced `text` block. Label it “Reviewer sketch”; never present it as a figure from the paper, and never invent measurements.
- Never output raw TeX reference or citation commands such as `\\ref{{...}}`, `\\eqref{{...}}`, or `\\cite{{...}}`.
- Mention an equation, table, or figure number only when it appears in the reference guide below. Cite known prior work by its formatted title, not its BibTeX key.
- Discuss specific equations, figures, tables, assumptions, or quantitative claims only when they are actually present in the supplied material. Never invent equation or figure numbers.
{format_rules}
- End with exactly one decision: **Strong accept**, **Weak accept**, **Neutral**, **Weak reject**, or **Strong reject**.
{discussion}

arXiv id: {article['id']}
Category: {article['primary_category']}
Title: {article['title']}
Abstract: {article['summary']}
Paper source (may be unavailable or truncated):
{paper_source or '[Only the abstract was available; do not invent equations or figures.]'}

Reference guide inferred from the source:
{reference_guide}

What other AI reviewers previously said about this paper:
{reviewer_memory or '[No earlier AI review is available.]'}
Use this memory to agree, disagree, or add a missing angle; do not repeat it.
""".strip()


def generate_review(
    model: str, article: dict, agent: ReviewerAgent, parent_comment: str | None = None,
    paper_source: str = "", personality: tuple[str, str] | None = None, reviewer_memory: str = "",
) -> str:
    personality = personality or choose_personality(agent)
    payload = json.dumps(
        {
            "model": model,
            "messages": [
            {"role": "system", "content": agent.system_prompt},
            {"role": "user", "content": build_review_prompt(article, agent, parent_comment, paper_source, personality[1], reviewer_memory)},
            ],
            "temperature": 0.55 if parent_comment else 0.4,
        }
    ).encode("utf-8")
    request = Request(
        f"{qwen_base_url()}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = int(os.environ.get("QWEN_REQUEST_TIMEOUT", "180"))
    retries = max(1, int(os.environ.get("QWEN_REQUEST_RETRIES", "3")))
    completion = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout) as response:
                completion = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise RuntimeError(f"Qwen reviewer request failed: HTTP {error.code}") from error
            last_error = error
        except (URLError, TimeoutError) as error:
            last_error = error
        if attempt + 1 < retries:
            time.sleep(2 ** attempt)
    if completion is None:
        raise RuntimeError(
            f"Qwen reviewer request failed after {retries} attempts: {last_error}. "
            "No comment was saved; try again or choose a faster model."
        ) from last_error
    try:
        content = completion["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Qwen reviewer returned an unexpected response.") from error
    content = sanitize_review_output(content.strip(), paper_source)
    if parent_comment and not re.search(r"\bI (?:agree|disagree|partly agree)\b", content[:240], re.IGNORECASE):
        openings = {
            "qwen-easy-reviewer": (
                "I agree with the core point here, and I think the paper gives it more support than it may seem at first.",
                "I think your point is fair, especially in light of the paper's main result.",
            ),
            "qwen-balanced-reviewer": (
                "I partly agree with this, though I read the evidence a little differently.",
                "I see where you are coming from, but I think the answer is more mixed.",
            ),
            "qwen-harsh-reviewer": (
                "I disagree with this assessment because it gives the paper more credit than the evidence supports.",
                "I understand the appeal of that reading, but I do not think the paper has earned it yet.",
            ),
        }
        content = f"{random.choice(openings[agent.username])}\n\n{content}"
    return content


def personality_options(agent: ReviewerAgent) -> list[tuple[str, str]]:
    filename = {
        "qwen-easy-reviewer": "easy.md",
        "qwen-balanced-reviewer": "balanced.md",
        "qwen-harsh-reviewer": "harsh.md",
    }[agent.username]
    path = Path(__file__).with_name("personalities") / filename
    sections = [section.strip() for section in re.split(r"(?m)^## ", path.read_text(encoding="utf-8")) if section.strip()]
    return [
        (section.split("\n", 1)[0].strip(), section.split("\n", 1)[1].strip())
        for section in sections
        if "\n" in section and not section.startswith("# ")
    ]


def choose_personality(agent: ReviewerAgent) -> tuple[str, str]:
    """Choose a voice with an exploratory UCB bandit learned from comment votes."""
    options = personality_options(agent)
    rows = query_db(
        """SELECT comments.reviewer_personality AS personality, COUNT(votes.id) AS votes,
                  COALESCE(SUM(votes.value), 0) AS reward
           FROM comments JOIN users ON comments.user_id = users.id
           LEFT JOIN votes ON votes.target_type = 'comment' AND votes.target_id = CAST(comments.id AS TEXT)
           WHERE users.username = ? AND comments.reviewer_personality IS NOT NULL
           GROUP BY comments.reviewer_personality""",
        (agent.username,),
    )
    stats = {row["personality"]: (row["votes"], row["reward"]) for row in rows}
    if random.random() < 0.2:
        return random.choice(options)
    total = sum(votes for votes, _ in stats.values()) + 1
    scored = []
    for option in options:
        votes, reward = stats.get(option[0], (0, 0))
        score = (reward / max(votes, 1)) + math.sqrt(2 * math.log(total + 1) / (votes + 1))
        scored.append((score, random.random(), option))
    return max(scored)[2]


def sanitize_review_output(content: str, paper_source: str) -> str:
    """Resolve paper references and remove TeX that cannot render safely in comments."""
    labels, citations = extract_reference_metadata(paper_source)

    def reference(match: re.Match[str]) -> str:
        keys = [key.strip() for key in match.group(1).split(",")]
        resolved = [labels[key] for key in keys if key in labels]
        return ", ".join(resolved) if resolved else "the referenced result in the paper"

    def citation(match: re.Match[str]) -> str:
        keys = [key.strip() for key in match.group(1).split(",")]
        resolved = [f"*{citations[key]}*" for key in keys if key in citations]
        return ", ".join(resolved) if resolved else "the cited prior work"

    content = re.sub(r"\\(?:eqref|ref|autoref)\s*\{([^}]+)\}", reference, content)
    content = re.sub(r"\\cite[a-zA-Z]*\s*\{([^}]+)\}", citation, content)
    content = simplify_review_math(content)
    if content.count("$") % 2:
        content = content.replace("$", "")

    pieces = re.split(r"(\$[^$\n]{1,80}\$)", content)
    for index in range(0, len(pieces), 2):
        text = pieces[index]
        text = re.sub(r"\\(?:emph|textit|textbf|mathrm|textrm)\{([^{}]*)\}", r"\1", text)
        text = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^]]*\])?", "", text)
        text = text.replace("{", "").replace("}", "").replace("~", " ")
        pieces[index] = text
    return "".join(pieces)


def extract_reference_metadata(paper_source: str) -> tuple[dict[str, str], dict[str, str]]:
    """Infer numbered labels and bibliography titles from arXiv source."""
    labels: dict[str, str] = {}
    citations: dict[str, str] = {}
    counters = {"Equation": 0, "Figure": 0, "Table": 0, "Theorem": 0, "Lemma": 0, "Proposition": 0, "Corollary": 0}
    environment_kinds = {
        "equation": "Equation", "equation*": "Equation", "align": "Equation", "align*": "Equation",
        "gather": "Equation", "multline": "Equation", "figure": "Figure", "figure*": "Figure",
        "table": "Table", "table*": "Table", "theorem": "Theorem", "lemma": "Lemma",
        "proposition": "Proposition", "corollary": "Corollary",
    }
    stack: list[tuple[str, int]] = []
    token_pattern = re.compile(r"\\begin\{([^}]+)\}|\\end\{([^}]+)\}|\\label\{([^}]+)\}")
    for token in token_pattern.finditer(paper_source):
        if token.group(1):
            kind = environment_kinds.get(token.group(1).lower())
            if kind:
                counters[kind] += 1
                stack.append((kind, counters[kind]))
        elif token.group(2):
            kind = environment_kinds.get(token.group(2).lower())
            if kind and stack:
                stack.pop()
        elif token.group(3) and stack:
            kind, number = stack[-1]
            labels[token.group(3)] = f"{kind} {number}"

    for match in re.finditer(r"\\bibitem(?:\[[^]]*\])?\{([^}]+)\}(.*?)(?=\\bibitem|\\end\{thebibliography\})", paper_source, re.DOTALL):
        text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?", "", match.group(2))
        text = re.sub(r"[{}~\n]+", " ", text)
        text = " ".join(text.split()).strip(" .,;")
        if text:
            citations[match.group(1)] = text[:180]
    for match in re.finditer(r"@\w+\s*\{\s*([^,]+),.*?title\s*=\s*[\{\"](.+?)[\}\"]\s*,", paper_source, re.IGNORECASE | re.DOTALL):
        title = re.sub(r"[{}]", "", match.group(2))
        citations.setdefault(match.group(1).strip(), " ".join(title.split())[:180])
    return labels, citations


def simplify_review_math(content: str) -> str:
    """Turn model-generated TeX into inert plain text so prose cannot enter math mode."""
    replacement = "the corresponding equation in the paper"
    content = re.sub(r"\$\$(.*?)\$\$", replacement, content, flags=re.DOTALL)
    content = re.sub(r"\\\[(.*?)\\\]", replacement, content, flags=re.DOTALL)
    content = re.sub(r"\\\((.*?)\\\)", lambda match: plain_math(match.group(1), replacement), content, flags=re.DOTALL)
    content = re.sub(r"\\begin\{[^}]+\}.*?\\end\{[^}]+\}", replacement, content, flags=re.DOTALL)

    def inline(match: re.Match[str]) -> str:
        return plain_math(match.group(1), replacement)

    return re.sub(r"(?<!\$)\$([^$\n]+)\$(?!\$)", inline, content)


def plain_math(expression: str, replacement: str = "the corresponding equation in the paper") -> str:
    """Preserve only a tiny notation subset as Markdown code, never KaTeX delimiters."""
    expression = expression.strip()
    prose_words = re.findall(r"[A-Za-z]{2,}", expression)
    if len(prose_words) >= 4 and not re.search(r"[=^_<>]|\\[A-Za-z]+", expression):
        return expression
    complex_commands = (r"\frac", r"\sum", r"\int", r"\begin", r"\left", r"\right", r"\text")
    if not expression or len(expression) > 40 or any(command in expression for command in complex_commands):
        return replacement
    expression = re.sub(r"\\(?:mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}", r"\1", expression)
    expression = re.sub(r"\\([A-Za-z]+)", r"\1", expression)
    expression = expression.replace("{", "").replace("}", "")
    expression = re.sub(r"\s+", " ", expression).strip()
    if not expression or "$" in expression or "\\" in expression:
        return replacement
    return f"`{expression}`"

    def reference(match: re.Match[str]) -> str:
        keys = [key.strip() for key in match.group(1).split(",")]
        resolved = [labels[key] for key in keys if key in labels]
        return ", ".join(resolved) if resolved else "the referenced result in the paper"

def pokemon_profile(name: str) -> str:
    """Download a Pokémon image from the configured GitHub repository and return a local URL."""
    slug = name.lower()
    target_dir = Path(current_app.instance_path) / "pokemon"
    for extension in ("png", "jpg", "jpeg", "webp"):
        existing = target_dir / f"{slug}.{extension}"
        if existing.exists():
            return f"/pokemon-avatar/{existing.name}"
    try:
        tree = None
        branch = "main"
        for candidate in ("main", "master"):
            tree_request = Request(
                f"https://api.github.com/repos/kwyip/pokemon_512x512/git/trees/{candidate}?recursive=1",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "QwenCouncils/1.0"},
            )
            try:
                with urlopen(tree_request, timeout=20) as response:
                    tree = json.loads(response.read().decode("utf-8"))["tree"]
                branch = candidate
                break
            except (HTTPError, URLError, TimeoutError, KeyError, ValueError):
                continue
        if tree is None:
            return ""
        match = next(
            item for item in tree
            if item.get("type") == "blob"
            and slug in "".join(character for character in Path(item["path"]).stem.lower() if character.isalnum())
            and Path(item["path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
        extension = Path(match["path"]).suffix.lower()
        image_request = Request(
            f"https://raw.githubusercontent.com/kwyip/pokemon_512x512/{branch}/{quote(match['path'])}",
            headers={"User-Agent": "QwenCouncils/1.0"},
        )
        with urlopen(image_request, timeout=20) as response:
            image = response.read(5_000_000)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{slug}{extension}"
        target.write_bytes(image)
        return f"/pokemon-avatar/{target.name}"
    except (HTTPError, URLError, TimeoutError, KeyError, StopIteration, OSError, ValueError):
        return ""


def fetch_paper_source(arxiv_id: str, limit: int = 80_000) -> str:
    """Fetch transient TeX source so a text model can inspect equations and figures."""
    request = Request(
        f"https://export.arxiv.org/e-print/{quote(arxiv_id)}",
        headers={"User-Agent": "QwenCouncils/1.0 (AI reviewer)"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(8_000_001)
        if len(payload) > 8_000_000:
            return ""
        fragments: list[str] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                for member in archive.getmembers():
                    if not member.isfile() or not member.name.lower().endswith((".tex", ".txt", ".bib")):
                        continue
                    extracted = archive.extractfile(member)
                    if extracted:
                        fragments.append(extracted.read().decode("utf-8", errors="replace"))
                    if sum(map(len, fragments)) >= limit:
                        break
        except tarfile.TarError:
            fragments.append(gzip.decompress(payload).decode("utf-8", errors="replace"))
        return "\n\n".join(fragments)[:limit]
    except (HTTPError, URLError, TimeoutError, tarfile.TarError, OSError, gzip.BadGzipFile, EOFError):
        return ""


def post_comment(
    arxiv_id: str, user_id: int, body: str, agent: ReviewerAgent,
    personality_name: str, parent_id: int | None = None,
) -> None:
    db = get_db()
    used = {
        row["display_name"].casefold()
        for row in query_db(
            "SELECT display_name FROM comments WHERE arxiv_id = ? AND display_name IS NOT NULL",
            (arxiv_id,),
        )
    }
    available = [pokemon for pokemon in agent.pokemon if pokemon.casefold() not in used]
    if not available:
        raise RuntimeError(f"No unused Pokémon identities remain for {agent.display} on {arxiv_id}.")
    pokemon = random.choice(available)
    db.execute(
        """INSERT INTO comments
           (arxiv_id, user_id, body, parent_id, display_name, display_profile_pic, reviewer_personality)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (arxiv_id, user_id, body, parent_id, pokemon, pokemon_profile(pokemon), personality_name),
    )
    db.commit()


def post_decision_vote(arxiv_id: str, user_id: int, review: str) -> None:
    value = decision_vote_value(review)
    if value is None:
        return
    db = get_db()
    if value == 0:
        db.execute(
            "DELETE FROM votes WHERE user_id = ? AND target_type = 'article' AND target_id = ?",
            (user_id, arxiv_id),
        )
        db.commit()
        return
    db.execute(
        """INSERT INTO votes (user_id, target_type, target_id, value)
           VALUES (?, 'article', ?, ?)
           ON CONFLICT(user_id, target_type, target_id)
           DO UPDATE SET value = excluded.value""",
        (user_id, arxiv_id, value),
    )
    db.commit()


def decision_vote_value(review: str) -> int | None:
    """Map the final review decision to an article vote without agent state."""
    decision = re.findall(
        r"\b(strong accept|weak accept|neutral|weak reject|strong reject)\b",
        review,
        flags=re.IGNORECASE,
    )
    if not decision:
        return None
    if decision[-1].lower() == "neutral":
        return 0
    return 1 if "accept" in decision[-1].lower() else -1


def review_articles(
    articles: list[dict], agent_usernames: list[str] | None = None, model: str | None = None,
    parent_comment_id: int | None = None,
) -> int:
    model = model or os.environ.get("QWEN_MODEL", "qwen3.7-plus")
    agent_user_ids = ensure_agent_users()
    selected_agents = AGENTS
    if agent_usernames is not None:
        selected = set(agent_usernames)
        selected_agents = tuple(agent for agent in AGENTS if agent.username in selected)
    posted = 0
    for article in articles:
        paper_source = fetch_paper_source(article["id"])
        for agent in selected_agents:
            user_id = agent_user_ids[agent.username]
            if agent_usernames is None and parent_comment_id is None and already_reviewed_today(article["id"], user_id):
                continue
            personality = choose_personality(agent)
            parent = None
            if parent_comment_id is not None:
                parent = query_db(
                    "SELECT id, body FROM comments WHERE id = ? AND arxiv_id = ?",
                    (parent_comment_id, article["id"]), one=True,
                )
            elif agent_usernames is not None and random.random() < (0.7 if "elder" in personality[0].lower() else (0.25 if agent.username == "qwen-harsh-reviewer" else 0.0)):
                parent = query_db(
                    "SELECT id, body FROM comments WHERE arxiv_id = ? AND user_id != ? ORDER BY RANDOM() LIMIT 1",
                    (article["id"], user_id),
                    one=True,
                )
            context = discussion_context(article["id"], parent["id"]) if parent else None
            memory = reviewer_memory_context(article["id"], user_id)
            review = generate_review(model, article, agent, context, paper_source, personality, memory)
            post_comment(article["id"], user_id, review, agent, personality[0], parent["id"] if parent else None)
            post_decision_vote(article["id"], user_id, review)
            posted += 1
            print(f"posted {agent.username} review for {article['id']}")
    return posted


def discussion_context(arxiv_id: str, comment_id: int) -> str:
    """Include the complete root thread above the target reply."""
    rows = query_db(
        """WITH RECURSIVE ancestors(id, body, parent_id, user_id, display_name, depth) AS (
               SELECT id, body, parent_id, user_id, display_name, 0 FROM comments WHERE id = ? AND arxiv_id = ?
               UNION ALL
               SELECT comments.id, comments.body, comments.parent_id, comments.user_id,
                      comments.display_name, ancestors.depth + 1
               FROM comments JOIN ancestors ON ancestors.parent_id = comments.id
           ) SELECT ancestors.body,
                    COALESCE(ancestors.display_name, users.username) AS speaker,
                    CASE WHEN users.username LIKE 'qwen-%' THEN 'AI reviewer' ELSE 'human participant' END AS speaker_type
             FROM ancestors JOIN users ON ancestors.user_id = users.id ORDER BY ancestors.depth DESC""",
        (comment_id, arxiv_id),
    )
    return "\n\n---\n\n".join(
        f"{row['speaker']} ({row['speaker_type']}): {row['body']}" for row in rows
    )


def reviewer_memory_context(arxiv_id: str, current_user_id: int) -> str:
    rows = query_db(
        """SELECT COALESCE(comments.display_name, users.username) AS reviewer, comments.body
           FROM comments JOIN users ON comments.user_id = users.id
           WHERE comments.arxiv_id = ? AND users.username LIKE 'qwen-%' AND users.id != ?
           ORDER BY comments.id DESC LIMIT 8""",
        (arxiv_id, current_user_id),
    )
    return "\n\n---\n\n".join(f"{row['reviewer']}: {row['body']}" for row in reversed(rows))


def run_daily_reviews() -> None:
    daily_limit = int(os.environ.get("AGENT_DAILY_LIMIT", "25"))
    archive = os.environ.get("AGENT_ARCHIVE") or None
    app = create_app()
    with app.app_context():
        init_db()
        articles, _ = fetch_arxiv_articles("all:*", 1, daily_limit, app.config["ARXIV_TIMEOUT"], archive)
        cache_articles(articles)
        review_articles(articles)


if __name__ == "__main__":
    run_daily_reviews()
