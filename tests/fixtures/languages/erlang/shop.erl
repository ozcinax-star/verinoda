-module(shop).
-behaviour(gen_server).
-include("shop.hrl").
-export([checkout/1]).

checkout(Items) ->
    Total = cart:total(Items),
    apply_discount(Total).

apply_discount(Total) when Total > 100 ->
    Total - 10;
apply_discount(Total) ->
    Total.
