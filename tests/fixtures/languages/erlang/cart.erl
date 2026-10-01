-module(cart).
-export([total/1]).

total(Items) ->
    lists:sum([price(I) || I <- Items]).

price({_Name, Price}) ->
    Price.
