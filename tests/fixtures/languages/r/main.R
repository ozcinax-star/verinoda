source("utils.R")

summarise_scores <- function(scores) {
  cleaned <- drop_missing(scores)
  mean(cleaned)
}

report <- function(scores) {
  print(summarise_scores(scores))
}
