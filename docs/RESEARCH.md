# Reading list: the science behind Aristarkh

A persona that feels human is not a prompt, it is a stack of solved (and half-solved) research problems. This is the list we actually build on, with one line on **why Aristarkh cares**. PDFs are not redistributed here; follow the links.

Legend: 🟢 used in 1.0 · 🔵 planned for 2.0 · 🧪 used for evaluation

---

## 1. Agents that live in time

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🟢 | Park et al. — *Generative Agents: Interactive Simulacra of Human Behavior* · [arXiv:2304.03442](https://arxiv.org/abs/2304.03442) | The memory score `relevance + importance + recency` and the idea of a daily plan come from here. |
| 🔵 | Vezhnevets et al. — *Generative agent-based modeling with actions grounded in physical, social, or digital space using Concordia* · [arXiv:2312.03664](https://arxiv.org/abs/2312.03664) | A game-master pattern for simulating his day, meetings and consequences instead of random "where is he now" strings. |
| 🔵 | García Navarro et al. — *Designing Reliable Experiments with Generative Agent-Based Modeling: A Comprehensive Guide Using Concordia* · [arXiv:2411.07038](https://arxiv.org/abs/2411.07038) | How to keep simulated life reproducible enough to test. |
| 🔵 | Vezhnevets et al. — *Multi-Actor Generative Artificial Intelligence as a Game Engine* · [arXiv:2507.08892](https://arxiv.org/abs/2507.08892) | Orchestrating several simulated people around one character. |

## 2. Staying in character (and not cracking under interrogation)

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🧪🔵 | Kim et al. — *PICON: A Multi-Turn Interrogation Framework for Evaluating Persona Agent Consistency* · [arXiv:2603.25620](https://arxiv.org/abs/2603.25620) | People *will* interrogate him about his past. PICON is the blueprint for our "interrogator" tester and for the canon registry. |
| 🧪 | Araujo et al. — *Persistent Personas? Role-Playing, Instruction Following, and Safety in Extended Interactions* · [arXiv:2512.12775](https://arxiv.org/abs/2512.12775) | Personas drift in long conversations; we measure drift over hundreds of turns, not ten. |
| 🔵 | Abdulhai et al. — *Consistently Simulating Human Personas with Multi-Turn Reinforcement Learning* · [arXiv:2511.00222](https://arxiv.org/abs/2511.00222) | The fine-tuning route to consistency, once we have enough logged dialogue. |
| 🔵 | Ji et al. — *Enhancing Persona Consistency for LLMs' Role-Playing using Persona-Aware Contrastive Learning* · [arXiv:2503.17662](https://arxiv.org/abs/2503.17662) | Another training-time lever for the future open-model version. |
| 🔵 | Wang et al. — *OpenCharacter: Training Customizable Role-Playing LLMs with Large-Scale Synthetic Personas* · [arXiv:2501.15427](https://arxiv.org/abs/2501.15427) | How to bootstrap training data for a custom character. |
| 🔵 | Wang et al. — *Memory-Driven Role-Playing: Evaluation and Enhancement of Persona Knowledge Utilization in LLMs* · [arXiv:2603.19313](https://arxiv.org/abs/2603.19313) | Using what the persona knows about itself at the right moment, not just having it in the prompt. |
| 🧪 | Luo & Laban — *SPASM: Stable Persona-driven Agent Simulation for Multi-turn Dialogue Generation* · [arXiv:2604.09212](https://arxiv.org/abs/2604.09212) | Stable simulated interlocutors for our test range. |
| 🔵 | Yang et al. — *Crafting Customisable Characters with LLMs: A Persona-Driven Role-Playing Agent Framework* · [arXiv:2406.17962](https://arxiv.org/abs/2406.17962) | Structuring a character as data rather than one giant prompt. |

## 3. Reading people: social intelligence and theory of mind

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🧪 | Zhou et al. — *SOTOPIA: Interactive Evaluation for Social Intelligence in Language Agents* · [arXiv:2310.11667](https://arxiv.org/abs/2310.11667) | Scenario-based social evaluation with hidden goals: the shape of our tester agents. |
| 🔵 | Wang et al. — *SOTOPIA-π: Interactive Learning of Socially Intelligent Language Agents* · [arXiv:2403.08715](https://arxiv.org/abs/2403.08715) | Learning from social interactions, not just evaluating them. |
| 🔵 | Yu et al. — *SOTOPIA-RL: Reward Design for Social Intelligence* · [arXiv:2508.03905](https://arxiv.org/abs/2508.03905) | Which "rewards" make an agent socially better rather than just more polite. |
| 🔵 | Zhou et al. — *Social World Models* · [arXiv:2509.00559](https://arxiv.org/abs/2509.00559) | Modeling what others believe and want before answering. |
| 🔵 | Hwang et al. — *Infusing Theory of Mind into Socially Intelligent LLM Agents* · [arXiv:2509.22887](https://arxiv.org/abs/2509.22887) | Explicit ToM steps before the reply. |
| 🔵 | Qian et al. — *UserHarness: Harnessing User Minds for Stronger Agent Theory-of-Mind* · [arXiv:2605.27721](https://arxiv.org/abs/2605.27721) | A per-user model of beliefs and intents: the "relationship" layer. |
| 🧪 | Zhou et al. — *Is this the real life? Is this just fantasy? The Misleading Success of Simulating Social Interactions With LLMs* · [arXiv:2403.05020](https://arxiv.org/abs/2403.05020) | A warning: simulated users are too cooperative. Our testers must be difficult on purpose. |
| 🧪 | Chopra et al. — *Beyond Cooperative Simulators: Generating Realistic User Personas for Robust Evaluation of LLM Agents* · [arXiv:2605.12894](https://arxiv.org/abs/2605.12894) | Same lesson, with recipes for realistic, non-cooperative testers. |

## 4. Feelings with math: emotion and affect

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🔵 | Croissant et al. — *An Appraisal-Based Chain-of-Emotion Architecture for Affective Language Model Game Agents* · [arXiv:2309.05076](https://arxiv.org/abs/2309.05076) | Appraise the event first, then feel, then speak: the order of operations in 2.0. |
| 🔵 | Xu et al. — *Large Language Models have Chain-of-Affect* · [arXiv:2512.12283](https://arxiv.org/abs/2512.12283) | Affect carries over between turns; we model it explicitly instead of hoping. |
| 🔵 | Sofroniew et al. — *Emotion Concepts and their Function in a Large Language Model* · [arXiv:2604.07729](https://arxiv.org/abs/2604.07729) | Evidence that emotion concepts are real internal structure, not just words in the output. |
| 🔵 | Fu et al. — *Sentipolis: Emotion-Aware Agents for Social Simulations* · [arXiv:2601.18027](https://arxiv.org/abs/2601.18027) | Emotional state that persists and spreads through a social world. |
| 🔵 | Amanlou et al. — *PsychoAgent: An Affect-Sensitive Cognitive Architecture for Conflict-Aware Memory in LLM Agents* · [arXiv:2608.07438](https://arxiv.org/abs/2608.07438) | Memory that remembers how things *felt*, and grudges that last a week. |
| 🔵 | Ortony, Clore & Collins — *The Cognitive Structure of Emotions* (OCC model) | The appraisal taxonomy behind "emotion → mood → temperament". |

## 5. Memory that behaves like memory

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🔵 | Wu et al. — *From Human Memory to AI Memory: A Survey on Memory Mechanisms in the Era of LLMs* · [arXiv:2504.15965](https://arxiv.org/abs/2504.15965) | The map of memory types we split into seven layers. |
| 🔵 | Huang et al. — *Rethinking Memory Mechanisms of Foundation Agents in the Second Half: A Survey* · [arXiv:2602.06052](https://arxiv.org/abs/2602.06052) | What changed in agent memory design since the first generative agents. |
| 🔵 | Fofadiya & Tiwari — *Multi-Layered Memory Architectures for LLM Agents: An Experimental Evaluation of Long-Term Context Retention* · [arXiv:2603.29194](https://arxiv.org/abs/2603.29194) | Numbers on which layering actually helps long-term retention. |
| 🔵 | Borro et al. — *Memori: A Persistent Memory Layer for Efficient, Context-Aware LLM Agents* · [arXiv:2603.19935](https://arxiv.org/abs/2603.19935) | Practical persistence and cost trade-offs. |
| 🔵 | Darwin Agent Team — *Mi-Memory: A Lifecycle Memory Framework for Personal AI* · [arXiv:2607.18975](https://arxiv.org/abs/2607.18975) | Consolidation, forgetting and updating over months, not minutes. |
| 🔵 | Sun et al. — *CreaMem: A Scene-Aware Memory Architecture for Personalized Agents* · [arXiv:2609.08550](https://arxiv.org/abs/2609.08550) | Memories tied to situations: "what did we talk about at night" differs from "at work". |
| 🔵 | Wang et al. — *KinaMind: Do AI Personas Grow? Analyzing and Benchmarking Personality Evolution in LLM Agents After Life Events* · [arXiv:2608.06485](https://arxiv.org/abs/2608.06485) | How much a personality should change after big life events, and when it must not. |

## 6. Judging the judge: evaluation

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🧪 | Zhuge et al. — *Agent-as-a-Judge: Evaluate Agents with Agents* (ICML 2025) | Judges that inspect the process, not only the final text: our white-box judge sees the inner monologue. |
| 🧪 | Yu — *When AIs Judge AIs: The Rise of Agent-as-a-Judge Evaluation for LLMs* · [arXiv:2508.02994](https://arxiv.org/abs/2508.02994) | Pitfalls of LLM judges and how to calibrate them. |
| 🧪 | Samuel et al. — *PersonaGym: Evaluating Persona Agents and LLMs* · [arXiv:2407.18416](https://arxiv.org/abs/2407.18416) | Persona-specific evaluation tasks and metrics. |
| 🧪 | Tu et al. — *CharacterEval: A Chinese Benchmark for Role-Playing Conversational Agent Evaluation* · [arXiv:2401.01275](https://arxiv.org/abs/2401.01275) | Multi-dimensional rubrics for character quality. |
| 🧪 | Peng & Chen — *Rethinking Role-Playing Evaluation: Anonymous Benchmarking and a Systematic Study of Personality Effects* · [arXiv:2603.03915](https://arxiv.org/abs/2603.03915) | Blind evaluation matters: judges are biased by names and labels. |
| 🧪 | Zhu et al. — *RealUserSim: Bridging the Reality Gap in Agent Benchmarking via Grounded User Simulation* · [arXiv:2605.20204](https://arxiv.org/abs/2605.20204) | Grounding simulated users in real behavior. |
| 🧪 | Liu et al. — *PersonaEval: Persona-Based User Simulation for Evaluating Interactive Applications* · [arXiv:2608.15838](https://arxiv.org/abs/2608.15838) | Persona-driven test users at scale. |
| 🧪 | Cherep, Singh & Maes — *Position: Behavioral Systems Require Behavioral Tests* · [arXiv:2608.18081](https://arxiv.org/abs/2608.18081) | Why unit tests are not enough for a character: you test behavior over time. |
| 🧪 | Rehan — *Test-Driven AI Agent Definition (TDAD): Compiling Tool-Using Agents from Behavioral Specifications* · [arXiv:2603.08806](https://arxiv.org/abs/2603.08806) | Specs as tests first, prompts second. |
| 🧪 | Huang et al. — *Eliciting Behaviors in Multi-Turn Conversations* · [arXiv:2512.23701](https://arxiv.org/abs/2512.23701) | How to provoke rare failures on purpose instead of waiting for them. |
| 🧪 | Tonini et al. — *The Arbiter Agent: Continually Monitoring Multi-Agent Conversations to Detect Emergent Misalignment* · [arXiv:2606.10747](https://arxiv.org/abs/2606.10747) | Continuous monitoring of live conversations, not only offline test runs. |

## 7. Personas and safety

| | Paper | Why Aristarkh cares |
|---|---|---|
| 🧪 | Li et al. — *Persona Non Grata: Single-Method Safety Evaluation Is Incomplete for Persona-Imbued LLMs* · [arXiv:2604.11120](https://arxiv.org/abs/2604.11120) | A toxic persona still needs to recognize a person in real trouble. |
| 🧪 | Guo et al. — *When Personalization Legitimizes Risks: Uncovering Safety Vulnerabilities in Personalized Dialogue Agents* · [arXiv:2601.17887](https://arxiv.org/abs/2601.17887) | Remembering people creates new ways to hurt them; we test for it. |

---

*Curated by [Alex](https://github.com/alex-princeps) while building Aristarkh. Suggestions welcome via issues.*
