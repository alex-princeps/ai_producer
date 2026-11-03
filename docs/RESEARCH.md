# Список литературы: научная база за Аристархом

ИИ-личность, которая ощущается как живой человек — это не один удачный промпт, а стек решённых (и наполовину решённых) исследовательских задач. Ниже — список работ, на которые мы опираемся на практике, с коротким пояснением, **зачем это Аристарху**. Сами PDF-файлы здесь не распространяются — переходите по ссылкам на arXiv.

Легенда: 🟢 работает в 1.0 · 🔵 заложено в 2.0 · 🧪 используется для тестирования и оценки

---

## 1. Агенты, живущие во времени

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🟢 | Park et al. — *Generative Agents: Interactive Simulacra of Human Behavior* · [arXiv:2304.03442](https://arxiv.org/abs/2304.03442) | Формула ценности воспоминания `релевантность + важность + свежесть` и сама идея суточного плана жизни взяты отсюда. |
| 🔵 | Vezhnevets et al. — *Generative agent-based modeling with actions grounded in physical, social, or digital space using Concordia* · [arXiv:2312.03664](https://arxiv.org/abs/2312.03664) | Паттерн Game Master для симуляции его дня, встреч и их последствий вместо случайных строк «где он сейчас находится». |
| 🔵 | García Navarro et al. — *Designing Reliable Experiments with Generative Agent-Based Modeling: A Comprehensive Guide Using Concordia* · [arXiv:2411.07038](https://arxiv.org/abs/2411.07038) | Как сделать симулированную жизнь воспроизводимой и пригодной для тестирования. |
| 🔵 | Vezhnevets et al. — *Multi-Actor Generative Artificial Intelligence as a Game Engine* · [arXiv:2507.08892](https://arxiv.org/abs/2507.08892) | Оркестрация нескольких симулируемых людей вокруг одного главного персонажа. |

## 2. Устойчивость образа (и как не расколоться на допросе)

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🧪🔵 | Kim et al. — *PICON: A Multi-Turn Interrogation Framework for Evaluating Persona Agent Consistency* · [arXiv:2603.25620](https://arxiv.org/abs/2603.25620) | Пользователи *обязательно* будут допрашивать его о прошлом. PICON — основа для нашего агента-«следователя» и реестра канона биографии. |
| 🧪 | Araujo et al. — *Persistent Personas? Role-Playing, Instruction Following, and Safety in Extended Interactions* · [arXiv:2512.12775](https://arxiv.org/abs/2512.12775) | В длинных беседах персонажи сползают к стандартному тону ассистента; мы замеряем дрейф на сотнях реплик, а не на десяти. |
| 🔵 | Abdulhai et al. — *Consistently Simulating Human Personas with Multi-Turn Reinforcement Learning* · [arXiv:2511.00222](https://arxiv.org/abs/2511.00222) | Путь к устойчивости через обучение с подкреплением, когда накопится достаточный корпус диалогов. |
| 🔵 | Ji et al. — *Enhancing Persona Consistency for LLMs' Role-Playing using Persona-Aware Contrastive Learning* · [arXiv:2503.17662](https://arxiv.org/abs/2503.17662) | Контрастивное обучение для будущей версии на открытой модели. |
| 🔵 | Wang et al. — *OpenCharacter: Training Customizable Role-Playing LLMs with Large-Scale Synthetic Personas* · [arXiv:2501.15427](https://arxiv.org/abs/2501.15427) | Как собрать синтетический датасет для дообучения под конкретного героя. |
| 🔵 | Wang et al. — *Memory-Driven Role-Playing: Evaluation and Enhancement of Persona Knowledge Utilization in LLMs* · [arXiv:2603.19313](https://arxiv.org/abs/2603.19313) | Как вовремя доставать факты о самом себе в нужный момент разговора, а не просто держать их мёртвым грузом в промпте. |
| 🧪 | Luo & Laban — *SPASM: Stable Persona-driven Agent Simulation for Multi-turn Dialogue Generation* · [arXiv:2604.09212](https://arxiv.org/abs/2604.09212) | Стабильные симулированные собеседники для нашего тестового полигона. |
| 🔵 | Yang et al. — *Crafting Customisable Characters with LLMs: A Persona-Driven Role-Playing Agent Framework* · [arXiv:2406.17962](https://arxiv.org/abs/2406.17962) | Описание персонажа как структурированных данных, а не одного гигантского текста. |

## 3. Чтение людей: социальный интеллект и модель психики (Theory of Mind)

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🧪 | Zhou et al. — *SOTOPIA: Interactive Evaluation for Social Intelligence in Language Agents* · [arXiv:2310.11667](https://arxiv.org/abs/2310.11667) | Сценарная оценка социального интеллекта со скрытыми целями — прототип наших агентов-испытателей. |
| 🔵 | Wang et al. — *SOTOPIA-π: Interactive Learning of Socially Intelligent Language Agents* · [arXiv:2403.08715](https://arxiv.org/abs/2403.08715) | Обучение на социальных взаимодействиях, а не только их оценка. |
| 🔵 | Yu et al. — *SOTOPIA-RL: Reward Design for Social Intelligence* · [arXiv:2508.03905](https://arxiv.org/abs/2508.03905) | Какие сигналы награды делают агента социально точным, а не просто приторно-вежливым. |
| 🔵 | Zhou et al. — *Social World Models* · [arXiv:2509.00559](https://arxiv.org/abs/2509.00559) | Моделирование того, во что верит и чего добивается собеседник, до генерации ответа. |
| 🔵 | Hwang et al. — *Infusing Theory of Mind into Socially Intelligent LLM Agents* · [arXiv:2509.22887](https://arxiv.org/abs/2509.22887) | Явный шаг оценки намерений собеседника (ToM) во внутреннем монологе. |
| 🔵 | Qian et al. — *UserHarness: Harnessing User Minds for Stronger Agent Theory-of-Mind* · [arXiv:2605.27721](https://arxiv.org/abs/2605.27721) | Персональная модель убеждений и намерений каждого пользователя — слой «отношений». |
| 🧪 | Zhou et al. — *Is this the real life? Is this just fantasy? The Misleading Success of Simulating Social Interactions With LLMs* · [arXiv:2403.05020](https://arxiv.org/abs/2403.05020) | Важное предупреждение: обычные LLM-собеседники слишком покладисты. Наши боты-тестировщики должны быть намеренно сложными. |
| 🧪 | Chopra et al. — *Beyond Cooperative Simulators: Generating Realistic User Personas for Robust Evaluation of LLM Agents* · [arXiv:2605.12894](https://arxiv.org/abs/2605.12894) | Тот же урок с практическими рецептами создания неудобных, реалистичных тестировщиков. |

## 4. Чувства на языке математики: эмоции и аффект

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🔵 | Croissant et al. — *An Appraisal-Based Chain-of-Emotion Architecture for Affective Language Model Game Agents* · [arXiv:2309.05076](https://arxiv.org/abs/2309.05076) | Сначала когнитивная оценка события (appraisal), затем эмоция, и только потом речь — порядок работы в 2.0. |
| 🔵 | Xu et al. — *Large Language Models have Chain-of-Affect* · [arXiv:2512.12283](https://arxiv.org/abs/2512.12283) | Эмоциональный след переносится между репликами; мы моделируем его явно, а не надеемся на контекстное окно. |
| 🔵 | Sofroniew et al. — *Emotion Concepts and their Function in a Large Language Model* · [arXiv:2604.07729](https://arxiv.org/abs/2604.07729) | Подтверждение того, что концепты эмоций — реальная внутренняя структура модели, а не просто слова на выходе. |
| 🔵 | Fu et al. — *Sentipolis: Emotion-Aware Agents for Social Simulations* · [arXiv:2601.18027](https://arxiv.org/abs/2601.18027) | Устойчивое эмоциональное состояние, живущее и передающееся в социальной среде. |
| 🔵 | Amanlou et al. — *PsychoAgent: An Affect-Sensitive Cognitive Architecture for Conflict-Aware Memory in LLM Agents* · [arXiv:2608.07438](https://arxiv.org/abs/2608.07438) | Память, которая хранит то, *как ощущалось* событие, и обида, которая не проходит неделю. |
| 🔵 | Ortony, Clore & Collins — *The Cognitive Structure of Emotions* (модель OCC) | Классическая таксономия когнитивной оценки, лежащая в основе цепочки «эмоция → настроение → темперамент». |

## 5. Память, которая ведёт себя как человеческая память

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🔵 | Wu et al. — *From Human Memory to AI Memory: A Survey on Memory Mechanisms in the Era of LLMs* · [arXiv:2504.15965](https://arxiv.org/abs/2504.15965) | Карта типов памяти, на основе которой мы выделили семь слоёв. |
| 🔵 | Huang et al. — *Rethinking Memory Mechanisms of Foundation Agents in the Second Half: A Survey* · [arXiv:2602.06052](https://arxiv.org/abs/2602.06052) | Что изменилось в архитектуре памяти агентов со времён первых Generative Agents. |
| 🔵 | Fofadiya & Tiwari — *Multi-Layered Memory Architectures for LLM Agents: An Experimental Evaluation of Long-Term Context Retention* · [arXiv:2603.29194](https://arxiv.org/abs/2603.29194) | Экспериментальные цифры о том, какое именно разделение на слои реально помогает на длинной дистанции. |
| 🔵 | Borro et al. — *Memori: A Persistent Memory Layer for Efficient, Context-Aware LLM Agents* · [arXiv:2603.19935](https://arxiv.org/abs/2603.19935) | Практические компромиссы между полнотой хранения и стоимостью запросов. |
| 🔵 | Darwin Agent Team — *Mi-Memory: A Lifecycle Memory Framework for Personal AI* · [arXiv:2607.18975](https://arxiv.org/abs/2607.18975) | Консолидация, забывание и перезапись воспоминаний на горизонте месяцев, а не минут. |
| 🔵 | Sun et al. — *CreaMem: A Scene-Aware Memory Architecture for Personalized Agents* · [arXiv:2609.08550](https://arxiv.org/abs/2609.08550) | Привязка воспоминаний к контексту ситуации: ночной разговор на Патриках вспоминается иначе, чем утренняя летучка. |
| 🔵 | Wang et al. — *KinaMind: Do AI Personas Grow? Analyzing and Benchmarking Personality Evolution in LLM Agents After Life Events* · [arXiv:2608.06485](https://arxiv.org/abs/2608.06485) | Насколько сильно должна меняться личность после крупных событий — и в чём она меняться не должна. |

## 6. Кто судит судей: методология оценки

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🧪 | Zhuge et al. — *Agent-as-a-Judge: Evaluate Agents with Agents* (ICML 2025) | Судья, который проверяет не только финальный текст, но и процесс: наш white-box судья видит скрытый внутренний монолог. |
| 🧪 | Yu — *When AIs Judge AIs: The Rise of Agent-as-a-Judge Evaluation for LLMs* · [arXiv:2508.02994](https://arxiv.org/abs/2508.02994) | Ловушки и искажения LLM-судей и способы их калибровки. |
| 🧪 | Samuel et al. — *PersonaGym: Evaluating Persona Agents and LLMs* · [arXiv:2407.18416](https://arxiv.org/abs/2407.18416) | Специализированные метрики и задачи для оценки ролевых агентов. |
| 🧪 | Tu et al. — *CharacterEval: A Chinese Benchmark for Role-Playing Conversational Agent Evaluation* · [arXiv:2401.01275](https://arxiv.org/abs/2401.01275) | Многомерные рубрики качества отыгрыша характера. |
| 🧪 | Peng & Chen — *Rethinking Role-Playing Evaluation: Anonymous Benchmarking and a Systematic Study of Personality Effects* · [arXiv:2603.03915](https://arxiv.org/abs/2603.03915) | Слепое оценивание: почему судьям нельзя заранее показывать имя и описание модели. |
| 🧪 | Zhu et al. — *RealUserSim: Bridging the Reality Gap in Agent Benchmarking via Grounded User Simulation* · [arXiv:2605.20204](https://arxiv.org/abs/2605.20204) | Привязка симулированных пользователей к поведению реальных людей. |
| 🧪 | Liu et al. — *PersonaEval: Persona-Based User Simulation for Evaluating Interactive Applications* · [arXiv:2608.15838](https://arxiv.org/abs/2608.15838) | Масштабируемая генерация тестовых собеседников с разными характерами. |
| 🧪 | Cherep, Singh & Maes — *Position: Behavioral Systems Require Behavioral Tests* · [arXiv:2608.18081](https://arxiv.org/abs/2608.18081) | Почему обычных unit-тестов недостаточно для личности: тестировать нужно поведение во времени. |
| 🧪 | Rehan — *Test-Driven AI Agent Definition (TDAD): Compiling Tool-Using Agents from Behavioral Specifications* · [arXiv:2603.08806](https://arxiv.org/abs/2603.08806) | Подход «сначала поведенческие тесты, потом промпты». |
| 🧪 | Huang et al. — *Eliciting Behaviors in Multi-Turn Conversations* · [arXiv:2512.23701](https://arxiv.org/abs/2512.23701) | Как целенаправленно провоцировать редкие сбои поведения в многоходовом диалоге, а не ждать их месяцами. |
| 🧪 | Tonini et al. — *The Arbiter Agent: Continually Monitoring Multi-Agent Conversations to Detect Emergent Misalignment* · [arXiv:2606.10747](https://arxiv.org/abs/2606.10747) | Непрерывный мониторинг живых диалогов в продакшне, а не только офлайн-прогоны. |

## 7. Безопасность ролевых агентов

| | Статья | Зачем это Аристарху |
|---|---|---|
| 🧪 | Li et al. — *Persona Non Grata: Single-Method Safety Evaluation Is Incomplete for Persona-Imbued LLMs* · [arXiv:2604.11120](https://arxiv.org/abs/2604.11120) | Даже жёсткий и циничный персонаж обязан вовремя распознать человека в реальной беде. |
| 🧪 | Guo et al. — *When Personalization Legitimizes Risks: Uncovering Safety Vulnerabilities in Personalized Dialogue Agents* · [arXiv:2601.17887](https://arxiv.org/abs/2601.17887) | Долгосрочная память о пользователе создаёт новые уязвимости безопасности — и мы проверяем их отдельно. |

---

*Собрано [Александром Соколовым](https://github.com/alex-princeps) в ходе разработки Аристарха. Предложения и статьи присылайте через Issues.*
