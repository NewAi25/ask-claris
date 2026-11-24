// Governance questions grounded in the curated model-risk database, tailored per persona.

export const ROLES = ["Compliance Officer", "Data Scientist", "Business User"] as const;
export type Role = (typeof ROLES)[number];

export const RECOMMENDED: Record<Role, string[]> = {
  "Compliance Officer": [
    "Is the model fair across segments on validation?",
    "Any disparate impact alerts?",
    "Does client CUST_00002 have active communication consent?",
    "How many consent records are withdrawn or expired?",
    "Which segments have fairness metrics that were not computed?",
    "What date ranges were used for train and validation?",
  ],
  "Data Scientist": [
    "What are the top 10 features driving dispute flags?",
    "Has performance drifted over the OOT months?",
    "Compare AUC_ROC across all three models on validation.",
    "How is maxSscript_l_150d calculated?",
    "Show the autoencoder's precision and recall by OOT month.",
    "Which model is best?",
  ],
  "Business User": [
    "Which model is best?",
    "What are the top 10 features driving dispute flags?",
    "How many clients are in each customer segment?",
    "How fair is the champion model on validation overall?",
    "How many disputes are in the validation period?",
  ],
};

// Always-visible quick chips under the Ask box.
export const QUICK_SUGGESTIONS: string[] = [
  "What are the top 10 features driving dispute flags?",
  "Is the model fair across segments on validation?",
  "Has performance drifted over the OOT months?",
];
